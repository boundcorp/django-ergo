"""Agents: coding agents (Codex, Claude Code, omp) a bot starts on a task and hears back from.

An agent runs a CLI on a subscription, never an API key, somewhere an
**agent manager** can reach: an Orca host today, later a host over ssh or a
local shell. A manager is a small class a plugin returns from
``BotPlugin.agent_managers()``::

    class MyManager(AgentManager):
        name = "mine"
        agents = ("codex", "claude")

        def start(self, ctx, spec): ...           # launch it; return a JSON handle
        def check(self, ctx, handle): ...         # one look: an AgentCheck
        def reply(self, ctx, handle, question_id, text): ...
        def stop(self, handle): ...               # optional
        def log(self, worker, handle): ...        # optional, the full recent output

``start(ctx, spec)`` here picks the agent for a tier (``bots.routing.pick_agent``),
asks the manager to launch it, and starts a polling Worker (``agent:<manager>``)
that calls ``check`` until the agent is done. Questions the agent asks reach the
chat as messages, answered with ``ergo_agent_reply``; its report is the worker's
result. The ``agents`` skill holds the tools: ``ergo_agent_start``,
``ergo_agent_reply`` and ``ergo_agent_stop``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.tools import ToolContext
    from django_ergo.bots.workers import WorkerContext
    from django_ergo.conversation.models import Worker

logger = logging.getLogger(__name__)

POLL_SECONDS = 120
STATUSES = ("running", "done", "failed")


@dataclass
class AgentSpec:
    """What to start: the brief, where, and which agent on which model."""

    brief: str
    workspace: str
    agent: str = ""
    model: str = ""
    effort: str = ""
    title: str = ""
    tier: str = ""


@dataclass
class AgentQuestion:
    """Something the agent asked; ``id`` is what ``reply`` answers."""

    id: str
    body: str
    subject: str = ""
    kind: str = "question"  # or "escalation"


@dataclass
class AgentCheck:
    """One look at a running agent."""

    status: str = "running"  # running, done or failed
    progress: str = ""
    report: Any = None  # the result when done (text or JSON)
    error: str = ""  # why, when failed
    questions: list[AgentQuestion] = field(default_factory=list)


class AgentManager:
    """Runs coding agents somewhere. Subclass it in a plugin (``agent_managers``)."""

    name: str = ""
    # The agents it can run; empty means any.
    agents: tuple[str, ...] = ()
    default_agent: str = "codex"
    # Whether starting, answering or stopping an agent asks the user first.
    requires_approval: bool = True
    poll_seconds: float = POLL_SECONDS
    # Where agents run, for the model ("Orca worktrees on devbox").
    where: str = ""

    def available(self, ctx: ToolContext) -> bool:
        """Whether this chat may use it."""
        return True

    def start(self, ctx: ToolContext, spec: AgentSpec) -> dict:
        """Launch the agent; return a JSON handle that ``check`` and ``reply`` take."""
        raise NotImplementedError

    def check(self, ctx: WorkerContext, handle: dict) -> AgentCheck:
        """One look at the agent. It may use ``ctx.activity`` and ``ctx.state``."""
        raise NotImplementedError

    def reply(self, ctx: ToolContext, handle: dict, question_id: str, text: str) -> str:
        """Answer one of the agent's questions."""
        msg = f"{self.name} agents can't be answered from here"
        raise ValueError(msg)

    def stop(self, handle: dict) -> str:
        """Stop the agent (the worker was cancelled). Returns what happened."""
        return f"{self.name} has no stop; the agent may keep running"

    def log(self, worker: Worker, handle: dict) -> dict | None:
        """The agent's recent output, read now (``BotPlugin.worker_log``'s shape)."""
        return None

    def worker_handle(self, worker: Worker) -> dict | None:
        """The handle of a worker this manager started under another function name
        (a plugin's own older workers); None if it isn't one."""
        return None

    def project(self, handle: dict) -> dict | None:
        """The repository the agent works in, as ``project_info(...)`` makes it;
        None if it can't tell. Costs groups agents by it."""
        return None


def project_info(remote: str, root: str, branch: str = "", host: str = "local") -> dict:
    """A project as the Costs page groups agents: keyed by the repository's
    remote (every worktree and host of a repo is one project), else by its
    folder on that host. The key matches ``agent_history.derive_project_identity``."""
    from django_ergo.conversation.agent_history import canonical_remote

    remote = canonical_remote(remote) if remote else ""
    root = root.rstrip("/")
    if remote:
        key = f"remote:{hashlib.sha256(remote.encode()).hexdigest()[:24]}"
        path = remote.split("://", 1)[-1].split("/", 1)[-1]
    else:
        key = f"local:{hashlib.sha256(f'{host}:{root}'.encode()).hexdigest()[:24]}"
        path = root
    return {
        "key": key,
        "name": path.rsplit("/", 1)[-1] or root or "(unknown)",
        "path": path,
        "remote": remote,
        "branch": branch,
    }


def project_of(worker: Worker) -> dict | None:
    """The project an agent worker recorded, if any."""
    handle = (worker.args or {}).get("handle") or {}
    found = handle.get("project") or (worker.state or {}).get("project")
    return found if isinstance(found, dict) and found.get("key") else None


def find_project(manager: AgentManager, handle: dict) -> dict | None:
    """``manager.project``, best effort: an unknown project never stops an agent."""
    try:
        return manager.project(handle)
    except Exception:  # noqa: BLE001 — the project is for reporting only
        logger.warning("Couldn't tell %s agent's project", manager.name, exc_info=True)
        return None


def fill_projects(registry=None, limit: int = 500) -> int:
    """Record the project of agent workers that have none yet (started before
    projects were recorded); returns how many it filled."""
    from django_ergo.bots.background import find_bot
    from django_ergo.conversation.models import Worker

    filled = 0
    workers = Worker.objects.filter(
        function__startswith="agent:"
    ) | Worker.objects.filter(function="orca:watch")
    for worker in workers.order_by("-created_at")[:limit]:
        if project_of(worker) is not None:
            continue
        try:
            found = worker_of(find_bot(worker.bot_name, registry), worker)
        except LookupError:
            continue
        if found is None:
            continue
        manager, handle = found
        handle = {
            **handle,
            "path": handle.get("path") or (worker.state or {}).get("worktree", ""),
        }
        project = find_project(manager, handle)
        if project is None:
            continue
        state = {**(worker.state or {}), "project": project}
        Worker.objects.filter(pk=worker.pk).update(state=state)
        filled += 1
    return filled


def managers(bot: Bot) -> dict[str, AgentManager]:
    """The bot's agent managers, by name, from its plugins."""
    found: dict[str, AgentManager] = {}
    for plugin in bot.plugins:
        for name, manager in plugin.agent_managers().items():
            if name in found:
                msg = f"Two plugins add an agent manager named {name!r}"
                raise ValueError(msg)
            manager.name = manager.name or name
            found[name] = manager
    return found


def manager_for(
    bot: Bot, name: str = "", ctx: ToolContext | None = None
) -> AgentManager:
    usable = {n: m for n, m in managers(bot).items() if ctx is None or m.available(ctx)}
    if name:
        if name not in usable:
            known = ", ".join(sorted(usable)) or "none"
            msg = f"No agent manager {name!r} here (managers: {known})"
            raise ValueError(msg)
        return usable[name]
    if len(usable) == 1:
        return next(iter(usable.values()))
    if not usable:
        msg = f"{bot.name} has no agent manager here"
        raise ValueError(msg)
    msg = f"Say which manager runs it: {', '.join(sorted(usable))}"
    raise ValueError(msg)


def worker_of(bot: Bot, worker: Worker) -> tuple[AgentManager, dict] | None:
    """The manager and handle behind a worker, if it runs an agent."""
    kind, _, name = worker.function.partition(":")
    found = managers(bot)
    if kind == "agent" and name in found:
        return found[name], dict((worker.args or {}).get("handle") or {})
    for manager in found.values():
        handle = manager.worker_handle(worker)
        if handle is not None:
            return manager, handle
    return None


def start(ctx: ToolContext, spec: AgentSpec, manager: str = "") -> Worker:
    """Start an agent and the Worker that watches it.

    With ``spec.tier``, the agent, model and effort come from providers.yaml's
    ``agents`` tiers, on whichever subscription has room."""
    if ctx.session is None:
        msg = "Agents belong to a chat"
        raise ValueError(msg)
    found = manager_for(ctx.bot, manager, ctx)
    choice = None
    if spec.tier:
        from django_ergo.bots.routing import pick_agent

        choice = pick_agent(ctx.bot.providers, spec.tier)
        spec.agent, spec.model, spec.effort = choice.agent, choice.model, choice.effort
    spec.agent = spec.agent or found.default_agent
    if found.agents and spec.agent not in found.agents:
        msg = f"{found.name} runs {', '.join(found.agents)}, not {spec.agent!r}" + (
            f" (picked for the {spec.tier} tier)" if spec.tier else ""
        )
        raise ValueError(msg)
    spec.title = (spec.title or spec.brief.strip().splitlines()[0])[:120]
    if choice is not None:
        from django_ergo.bots.routing import record_agent_pick

        record_agent_pick(
            ctx.bot.providers,
            spec.tier,
            choice,
            f"{found.name} agent · {spec.title}",
            ctx.session,
        )
    handle = found.start(ctx, spec)
    if project := find_project(found, handle):
        handle["project"] = project
    state = {
        "agent": spec.agent,
        "model": spec.model,
        "effort": spec.effort,
        "manager": found.name,
        "seen": [],
    }
    return ctx.workers.start(
        f"agent:{found.name}",
        title=f"{spec.agent}: {spec.title}",
        state=state,
        handle=handle,
    )


def watch(ctx: WorkerContext, manager: AgentManager, handle: dict):
    """One check of an agent: pass on new questions, finish with its report."""
    if project_of(ctx.worker) is None and "project_checked" not in ctx.state:
        # Started before projects were recorded: look it up once.
        ctx.state["project_checked"] = True
        if project := find_project(manager, handle):
            ctx.state["project"] = project
    check = manager.check(ctx, handle)
    if check.status not in STATUSES:
        msg = f"{manager.name} reported an unknown status {check.status!r}"
        raise ValueError(msg)
    seen = set(ctx.state.get("seen") or [])
    for question in check.questions:
        if question.id in seen:
            continue
        seen.add(question.id)
        subject = f": {question.subject}" if question.subject else ""
        ctx.tell(
            f"The agent sent a {question.kind}{subject}\n\n{question.body}\n\n"
            f"Answer it with ergo_agent_reply (worker_id {ctx.worker.pk}, "
            f"question_id {question.id})."
        )
    ctx.state["seen"] = sorted(seen)
    if ctx.stopping:
        try:
            ctx.progress(manager.stop(handle))
        except Exception as exc:  # noqa: BLE001 — cancelled either way
            ctx.progress(f"Couldn't stop it: {exc}")
        return None
    if check.status == "done":
        return check.report
    if check.status == "failed":
        raise RuntimeError(check.error or f"The {manager.name} agent failed")
    return ctx.again(manager.poll_seconds, progress=check.progress)


def watcher(manager: AgentManager):
    """The worker function for ``agent:<manager>``."""

    def watch_agent(ctx, handle: dict):
        return watch(ctx, manager, handle)

    return watch_agent


def describe_managers(bot: Bot, ctx: ToolContext) -> list[dict]:
    return [
        {
            "name": m.name,
            "agents": list(m.agents),
            "where": m.where,
            "asks_first": m.requires_approval,
        }
        for m in managers(bot).values()
        if m.available(ctx)
    ]


AGENTS_INSTRUCTIONS = """\
Coding agents (Codex, Claude Code, omp) run on a subscription for minutes to
hours. ergo_agent_start starts one on a self-contained brief: the target, the
change, constraints, who owns what, and how to prove it's done. Pass a configured
tier (small, medium, large, xlarge, or a custom name from providers.yaml) to let routing
pick the agent and model from a subscription with room. The chat shows it as a
worker; its questions and its final report arrive here as messages, so don't
wait or poll. Answer a question with ergo_agent_reply; ergo_agent_stop stops one.
"""


def agent_toolkit(bot: Bot, ctx: ToolContext):
    """The ``agents`` skill: start, answer and stop coding agents."""
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import bot_tool
    from django_ergo.bots.workers import WorkerStarter
    from django_ergo.bots.workers import cancel
    from django_ergo.bots.workers import describe

    usable = describe_managers(bot, ctx)
    if not usable:
        return FunctionToolkit([], ctx)
    names = [m["name"] for m in usable]
    approval = any(m["asks_first"] for m in usable)
    starter = WorkerStarter(bot, ctx.session)

    def find(worker_id: str) -> tuple[Worker, AgentManager, dict]:
        worker = next((w for w in starter.list() if str(w.pk) == str(worker_id)), None)
        found = worker_of(bot, worker) if worker is not None else None
        if found is None:
            msg = f"No agent worker {worker_id} in this chat"
            raise ValueError(msg)
        return worker, *found

    where = "; ".join(
        f"{m['name']}: {', '.join(m['agents']) or 'any agent'}"
        + (f" ({m['where']})" if m["where"] else "")
        for m in usable
    )

    @bot_tool(
        name="ergo_agent_start",
        takes_context=True,
        description=(
            "Start a coding agent on a task. It runs for minutes to hours; its questions "
            "and report come back to this chat as messages, so don't wait or poll. "
            f"Managers: {where}."
        ),
        parameters={
            "brief": {"type": "string", "description": "The task, self-contained"},
            "workspace": {
                "type": "string",
                "description": "Where it works, in the manager's terms (for Orca, a "
                "worktree selector: id:<repo-id>::<path> or path:<path>)",
            },
            "title": {"type": "string", "description": "A short title for the task"},
            "tier": {
                "type": "string",
                "description": "Pick agent, model and effort for a configured agents "
                "tier in providers.yaml (including custom names) from a subscription "
                "with room (replaces agent, model and effort)",
            },
            "agent": {"type": "string", "description": "codex, claude or omp"},
            "model": {
                "type": "string",
                "description": "Model id or provider/model selector",
            },
            "effort": {"type": "string", "description": "Reasoning effort"},
            "manager": {"type": "string", "enum": names},
        },
        required=["brief", "workspace"],
        requires_approval=approval,
    )
    def start_agent(  # noqa: PLR0913
        ctx: ToolContext,
        brief: str,
        workspace: str,
        title: str = "",
        tier: str = "",
        agent: str = "",
        model: str = "",
        effort: str = "",
        manager: str = "",
    ) -> dict:
        spec = AgentSpec(brief, workspace, agent, model, effort, title, tier)
        worker = start(ctx, spec, manager)
        return {
            **describe(worker),
            "agent": {
                k: getattr(spec, k) for k in ("agent", "model", "effort", "tier")
            },
        }

    @bot_tool(
        name="ergo_agent_reply",
        takes_context=True,
        description="Answer a question one of this chat's coding agents asked.",
        parameters={
            "worker_id": {"type": "string"},
            "question_id": {"type": "string"},
            "answer": {"type": "string"},
        },
        required=["worker_id", "question_id", "answer"],
        requires_approval=approval,
    )
    def reply_agent(
        ctx: ToolContext, worker_id: str, question_id: str, answer: str
    ) -> str:
        _, found, handle = find(worker_id)
        return found.reply(ctx, handle, question_id, answer)

    @bot_tool(
        name="ergo_agent_stop",
        takes_context=True,
        description="Stop one of this chat's coding agents and cancel its worker.",
        parameters={"worker_id": {"type": "string"}},
        required=["worker_id"],
        requires_approval=approval,
    )
    def stop_agent(ctx: ToolContext, worker_id: str) -> str:
        worker, _, _ = find(worker_id)
        return cancel(worker, bot)

    return FunctionToolkit(
        [t.__bot_tool__ for t in (start_agent, reply_agent, stop_agent)], ctx
    )
