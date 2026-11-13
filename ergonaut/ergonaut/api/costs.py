"""Spending: every structured call's tokens, priced with django_ergo.pricing.

    GET /api/costs?days=30

Totals by kind, with chat replies (bot turns) split by bot, plus totals by
model and by day. Tokens and cost are split into uncached input, cache writes,
cache reads and output, each priced at its own rate. Superusers see everyone's calls; others see their own.

``GET /api/sessions/{id}/usage?hours=6`` is one chat's token usage per
hour (rolling hours ending now) for the sparkline in the chat header: its own
structured calls, and separately the coding agents its workers ran.

A call that ran for a chat without being one of its turns (a browser task's
driver, a look at an attachment) has no session of its own but names the
chat in ``metadata["session"]``; it counts for that chat and its bot.

``private`` breaks recorded private pool API spend (``private_pools`` in
providers.yaml) down by pool, provider, exact model and bot, from the
per-request ``InferenceRequest`` ledger. It is not a reconciled invoice.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, timedelta

from django.db.models import CharField, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone
from django_ergo.conversation.chat_reply import CHAT_REPLY_KIND
from django_ergo.conversation.models import (
    AgentUsage,
    AgentUsageEvent,
    ConversationSession,
    InferenceOutcome,
    InferenceRequest,
    StructuredCall,
)
from django_ergo.pricing import call_cost_parts, price_for
from ninja import Router, Schema
from ninja.errors import HttpError

from ergonaut.api.auth import user_auth
from ergonaut.api.bots import uuid_or_404, visible_sessions

router = Router(tags=["costs"], auth=user_auth)


class Bucket(Schema):
    name: str
    calls: int
    # Uncached input only; cache writes and reads are counted separately.
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0  # part of output_tokens (billed as output)
    input_cost: float
    cache_write_cost: float
    cache_read_cost: float
    output_cost: float
    cost: float
    unpriced_calls: int
    subscription_calls: int


class DayOut(Schema):
    date: str
    cost: float
    calls: int


class UsageHeadline(Schema):
    threads: int
    tokens: int
    cache_hit: float
    main_chats: float
    subscription: float
    api_spend: float
    compaction: float


class UsageThread(Schema):
    id: str
    title: str
    bot: str
    model: str
    role: str
    subscription: bool
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    tokens: int
    cache_hit: float
    share: float
    cost: float


class UsageOut(Schema):
    headline: UsageHeadline
    threads: list[UsageThread]


class AgentHeadline(Schema):
    sessions: int
    tokens: int
    cache_hit: float
    projects: int = 0
    cost: float = 0.0  # API-equivalent list price; agents run on subscriptions
    priced_rows: int = 0
    unpriced_rows: int = 0
    billed_usd: float | None = None  # The usage aggregate is not an invoice.


class AgentUsageRow(Schema):
    worker_id: str
    worker_title: str
    agent: str
    model: str
    chat_id: str
    chat_title: str
    bot: str
    worker_status: str
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tokens: int
    cache_hit: float
    requests: int
    project_key: str = ""
    project: str = ""
    project_remote: str = ""
    branch: str = ""
    cost: float = 0.0
    priced: bool = False
    # Lifetime totals of this worker/model, selected because it was active in the window.
    first_at: str | None = None
    last_at: str | None = None
    spillover: bool = False  # began before the window: tokens are not window-clipped


class AgentProject(Schema):
    """Agent work in one repository: every worktree and clone of one remote."""

    key: str
    name: str
    remote: str
    workers: int
    agents: list[str]
    models: list[str]
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    tokens: int
    cache_hit: float
    requests: int
    cost: float
    unpriced_models: list[str]
    last_at: str | None


class AgentsOut(Schema):
    headline: AgentHeadline
    rows: list[AgentUsageRow]
    by_project: list[AgentProject] = []


class PrivateSpendRow(Schema):
    """API-billed private pool requests for one (pool, provider, model, bot)."""

    pool: str
    provider: str
    model: str
    bot: str
    billing_mode: str
    requests: int  # HTTP attempts recorded, including those refused before sending
    sent: int  # attempts that reached (or may have reached) the provider
    prompt_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0  # part of output_tokens
    cost: float  # priced attempts only
    unknown_cost_requests: int  # sent, but no price or no usage: cost unknown, not 0
    unsettled_reserved: float  # worst-case reservations still held for those


class PrivateSpendOut(Schema):
    cost: float
    unknown_cost_requests: int
    rows: list[PrivateSpendRow]


class UsageHour(Schema):
    start: str  # ISO time the hour starts; it ends at the next one's start (or ``until``)
    thread_tokens: int  # the chat's own calls: turns, compaction, titles
    agent_tokens: int  # coding agents run by the chat's workers


class SessionUsageOut(Schema):
    until: str
    hours: list[UsageHour]
    thread_tokens: int
    agent_tokens: int


class CostsOut(Schema):
    days: int
    since: str = ""
    until: str = ""
    timezone: str = "UTC"
    total: Bucket
    by_kind: list[Bucket]
    chat_reply_by_bot: list[Bucket]
    by_model: list[Bucket]
    by_day: list[DayOut]
    unpriced_models: list[str]
    agents: AgentsOut
    usage: UsageOut
    private: PrivateSpendOut
    estimate: EstimateOut


def _private_spend(requests) -> dict:
    rows: dict[tuple, dict] = {}
    for req in requests:
        key = (req.pool, req.provider, req.model, req.bot_name)
        row = rows.setdefault(
            key,
            {
                "pool": req.pool,
                "provider": req.provider,
                "model": req.model,
                "bot": req.bot_name,
                "billing_mode": req.billing_mode,
                "requests": 0,
                "sent": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cost": 0.0,
                "unknown_cost_requests": 0,
                "unsettled_reserved": 0.0,
            },
        )
        row["requests"] += 1
        if req.outcome == InferenceOutcome.NOT_SENT:
            continue
        row["sent"] += 1
        for field in ("prompt_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
            row[field] += getattr(req, field) or 0
        if req.cost_status == "priced" and req.cost_usd is not None:
            row["cost"] += float(req.cost_usd)
        else:
            row["unknown_cost_requests"] += 1
            row["unsettled_reserved"] += float(req.reserved_usd or 0)
    ordered = sorted(rows.values(), key=lambda r: (-r["cost"], r["pool"], r["model"], r["bot"]))
    return {
        "cost": sum(r["cost"] for r in ordered),
        "unknown_cost_requests": sum(r["unknown_cost_requests"] for r in ordered),
        "rows": ordered,
    }


# (bucket token key, StructuredCall field, Price attribute)
PARTS = (
    ("input", "input_tokens", "input"),
    ("cache_write", "cache_creation_input_tokens", "cache_write"),
    ("cache_read", "cache_read_input_tokens", "cache_read"),
    ("output", "output_tokens", "output"),
)


def _empty(name: str) -> dict:
    bucket = {
        "name": name,
        "calls": 0,
        "cost": 0.0,
        "unpriced_calls": 0,
        "subscription_calls": 0,
        "reasoning_tokens": 0,
    }
    for part, _, _ in PARTS:
        bucket[f"{part}_tokens"] = 0
        bucket[f"{part}_cost"] = 0.0
    return bucket


def call_costs(call: StructuredCall) -> dict[str, float] | None:
    """The call's cost per part (input, cache_write, cache_read, output), or None if unpriced:
    what was recorded request by request, else priced from its totals (older calls)."""
    return call_cost_parts(call)


def _add(bucket: dict, call: StructuredCall, costs: dict[str, float] | None) -> None:
    bucket["calls"] += 1
    if call.session and call.session.transport_type == "cli" and call.kind != "voice_transcription":
        bucket["subscription_calls"] += 1
    for part, field, _ in PARTS:
        bucket[f"{part}_tokens"] += getattr(call, field) or 0
    bucket["reasoning_tokens"] += getattr(call, "reasoning_tokens", 0) or 0
    if costs is None:
        bucket["unpriced_calls"] += 1
        return
    for part, value in costs.items():
        bucket[f"{part}_cost"] += value
    bucket["cost"] += sum(costs.values())


def _sorted(buckets: dict) -> list[dict]:
    return sorted(buckets.values(), key=lambda b: (-b["cost"], -b["calls"], b["name"]))


def _tokens(bucket: dict) -> int:
    return sum(bucket[f"{part}_tokens"] for part, _, _ in PARTS)


def _cache_hit(bucket: dict) -> float:
    denominator = bucket["input_tokens"] + bucket["cache_write_tokens"] + bucket["cache_read_tokens"]
    return bucket["cache_read_tokens"] / denominator if denominator else 0.0


def _for_chat(calls):
    """Calls that are a chat's turns, or ran for it (``metadata["session"]``)."""
    return calls.annotate(for_chat=KeyTextTransform("session", "metadata"))


def _of_chats(calls, sessions):
    """``calls`` (from ``_for_chat``) that belong to one of ``sessions``."""
    ids = sessions.annotate(text_id=Cast("id", CharField())).values("text_id")
    return calls.filter(Q(session__in=sessions) | Q(session__isnull=True, for_chat__in=ids))


def _chat_of(calls: list[StructuredCall]) -> dict[str, ConversationSession]:
    """The chats that calls without a session ran for, by id."""
    ids = {call.for_chat for call in calls if call.session is None and getattr(call, "for_chat", "")}
    found = ConversationSession.objects.filter(id__in=[i for i in ids if _is_uuid(i)])
    return {str(session.id): session for session in found}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _add_session(
    by_session: dict[str, dict],
    call: StructuredCall,
    parts: dict | None,
    session: ConversationSession | None = None,
) -> tuple[int, int]:
    session = call.session or session
    if session is None:
        return 0, 0
    session_id = str(session.id)
    if session_id not in by_session:
        meta = session.metadata or {}
        by_session[session_id] = {
            "id": session_id,
            "title": session.user_title or meta.get("title") or f"{session.bot_name or 'Bot'} main",
            "bot": session.bot_name or "(no bot)",
            "model": call.model_name or "(unknown)",
            "role": meta.get("bot_role") or "",
            "subscription": session.transport_type == "cli",
            "bucket": _empty(session_id),
        }
    row = by_session[session_id]
    _add(row["bucket"], call, parts)
    if call.model_name:
        row["model"] = call.model_name
    call_tokens = sum((getattr(call, field) or 0) for _, field, _ in PARTS)
    return (
        call_tokens if row["role"] != "thread" else 0,
        call_tokens if row["subscription"] else 0,
    )


def _usage(
    total: dict,
    by_session: dict[str, dict],
    main: int,
    subscription: int,
    compaction: int,
) -> dict:
    token_total = _tokens(total)
    threads = []
    for row in by_session.values():
        bucket = row.pop("bucket")
        row_tokens = _tokens(bucket)
        threads.append(
            {
                **row,
                **{f"{part}_tokens": bucket[f"{part}_tokens"] for part, _, _ in PARTS},
                "tokens": row_tokens,
                "cache_hit": _cache_hit(bucket),
                "share": row_tokens / token_total if token_total else 0.0,
                "cost": bucket["cost"],
            }
        )
    threads.sort(key=lambda row: (-row["share"], row["title"], row["id"]))
    return {
        "headline": {
            "threads": len(threads),
            "tokens": token_total,
            "cache_hit": _cache_hit(total),
            "main_chats": main / token_total if token_total else 0.0,
            "subscription": subscription / token_total if token_total else 0.0,
            "api_spend": total["cost"],
            "compaction": compaction / token_total if token_total else 0.0,
        },
        "threads": threads,
    }


AGENT_PARTS = ("input", "cache_write", "cache_read", "output")


def _agent_cost(usage: AgentUsage) -> float | None:
    """Estimate agent token cost, or None if token data or model price is missing."""
    if not any(getattr(usage, f"{part}_tokens") for part in AGENT_PARTS):
        return None
    price = price_for(usage.model)
    if price is None:
        return None
    return sum(getattr(usage, f"{part}_tokens") * getattr(price, part) for part in AGENT_PARTS) / 1_000_000


def _hit(row: dict) -> float:
    denominator = row["input_tokens"] + row["cache_write_tokens"] + row["cache_read_tokens"]
    return row["cache_read_tokens"] / denominator if denominator else 0.0


def _project_of(worker) -> dict:
    project = ((worker.state or {}).get("project") if worker else None) or {}
    return project if isinstance(project, dict) else {}


def _agents(rows: list[AgentUsage], since=None) -> dict:
    output = []
    projects: dict[str, dict] = {}
    for usage in rows:
        session = usage.session
        worker = usage.worker
        project = _project_of(worker)
        cost = _agent_cost(usage)
        row = {
            "worker_id": str(usage.worker_id or ""),
            "worker_title": worker.title if worker else "(deleted worker)",
            "agent": usage.agent,
            "model": usage.model,
            "chat_id": str(session.id),
            "chat_title": session.user_title
            or (session.metadata or {}).get("title")
            or f"{session.bot_name or 'Bot'} main",
            "bot": usage.bot_name,
            "worker_status": worker.status if worker else "deleted",
            **{f"{part}_tokens": getattr(usage, f"{part}_tokens") for part in AGENT_PARTS},
            "reasoning_tokens": usage.reasoning_tokens,
            "tokens": sum(getattr(usage, f"{part}_tokens") for part in AGENT_PARTS),
            "requests": usage.requests,
            "project_key": str(project.get("key") or ""),
            "project": str(project.get("name") or ""),
            "project_remote": str(project.get("remote") or ""),
            "branch": str(project.get("branch") or ""),
            "cost": cost or 0.0,
            "priced": cost is not None,
            "first_at": usage.first_at.isoformat() if usage.first_at else None,
            "last_at": usage.last_at.isoformat() if usage.last_at else None,
            "spillover": since is not None and (usage.first_at is None or usage.first_at < since),
        }
        row["cache_hit"] = _hit(row)
        output.append(row)
        key = row["project_key"]
        group = projects.setdefault(
            key,
            {
                "key": key,
                "name": row["project"] or "(no project recorded)",
                "remote": row["project_remote"],
                "workers": set(),
                "agents": set(),
                "models": set(),
                **{f"{part}_tokens": 0 for part in AGENT_PARTS},
                "requests": 0,
                "cost": 0.0,
                "unpriced_models": set(),
                "last_at": None,
            },
        )
        group["workers"].add(row["worker_id"] or f"deleted:{usage.pk}")
        group["agents"].add(usage.agent)
        group["models"].add(usage.model)
        for part in AGENT_PARTS:
            group[f"{part}_tokens"] += row[f"{part}_tokens"]
        group["requests"] += usage.requests
        group["cost"] += row["cost"]
        if cost is None:
            group["unpriced_models"].add(usage.model)
        if usage.last_at and (group["last_at"] is None or usage.last_at > group["last_at"]):
            group["last_at"] = usage.last_at
    output.sort(key=lambda row: (-row["tokens"], row["worker_title"], row["model"]))
    by_project = []
    for group in projects.values():
        group["tokens"] = sum(group[f"{part}_tokens"] for part in AGENT_PARTS)
        group["cache_hit"] = _hit(group)
        group["workers"] = len(group["workers"])
        for name in ("agents", "models", "unpriced_models"):
            group[name] = sorted(group[name])
        group["last_at"] = group["last_at"].isoformat() if group["last_at"] else None
        by_project.append(group)
    # Unattributed work last, whatever its size.
    by_project.sort(key=lambda g: (not g["key"], -g["tokens"], g["name"]))
    total = {f"{part}_tokens": sum(row[f"{part}_tokens"] for row in output) for part in AGENT_PARTS}
    return {
        "headline": {
            "sessions": len({row["worker_id"] for row in output}),
            "tokens": sum(row["tokens"] for row in output),
            "cache_hit": _hit(total),
            "projects": sum(1 for group in by_project if group["key"]),
            "cost": sum(row["cost"] for row in output),
            "priced_rows": sum(row["priced"] for row in output),
            "unpriced_rows": sum(not row["priced"] for row in output),
            "billed_usd": None,
        },
        "rows": output,
        "by_project": by_project,
    }


class EstimateScope(Schema):
    """Tokens and API list-price-equivalent cost for one population (bot calls, agent rows, both).

    Costs cover priced rows only; ``unpriced_*`` say what they leave out. A list-price
    estimate is not an invoice and not subscription quota use. ``cache_hit`` is
    cache_read / (uncached input + cache writes + cache reads), output excluded; None when
    that denominator is 0 (nothing recorded), which is not the same as a 0% hit rate."""

    rows: int
    input_tokens: int  # uncached input
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    tokens: int
    cache_denominator: int
    cache_hit: float | None
    list_cost: float
    input_cost: float
    cache_write_cost: float
    cache_read_cost: float
    output_cost: float
    priced_rows: int
    unpriced_rows: int
    priced_tokens: int
    unpriced_tokens: int
    unpriced_models: list[str]
    no_usage_rows: int  # rows with no token data; usage and cost are unknown
    exact_window: bool  # False: rows are lifetime totals selected by activity in the window
    spillover_rows: int = 0  # lifetime rows that began before ``since``
    spillover_tokens: int = 0
    first_at: str | None = None
    last_at: str | None = None


class EstimateOut(Schema):
    """List-price-equivalent view over the same window for bot calls and coding agents."""

    basis: str
    bot: EstimateScope
    agents: EstimateScope
    combined: EstimateScope


def _list_parts(call: StructuredCall) -> dict[str, float] | None:
    """Estimate aggregated tokens using model-table rates, never recorded cost provenance."""
    if call.kind == "voice_transcription":
        return None  # Its pricing is not token-based.
    price = price_for(call.model_name)
    if price is None:
        return None
    return price.parts(
        call.input_tokens,
        call.output_tokens,
        call.cache_creation_input_tokens,
        call.cache_read_input_tokens,
        one_request=False,
    )


def _scope() -> dict:
    return {
        "rows": 0,
        "tokens": 0,
        "list_cost": 0.0,
        "priced_rows": 0,
        "unpriced_rows": 0,
        "priced_tokens": 0,
        "unpriced_tokens": 0,
        "unpriced_models": set(),
        "no_usage_rows": 0,
        "spillover_rows": 0,
        "spillover_tokens": 0,
        "first_at": None,
        "last_at": None,
        **{f"{part}_tokens": 0 for part in AGENT_PARTS},
        **{f"{part}_cost": 0.0 for part in AGENT_PARTS},
    }


def _scope_add(scope: dict, counts: dict[str, int], parts: dict[str, float] | None, model: str) -> None:
    tokens = sum(counts.values())
    scope["rows"] += 1
    scope["tokens"] += tokens
    for part in AGENT_PARTS:
        scope[f"{part}_tokens"] += counts[part]
    if not tokens:
        scope["no_usage_rows"] += 1
        return
    if parts is None:
        scope["unpriced_rows"] += 1
        scope["unpriced_tokens"] += tokens
        scope["unpriced_models"].add(model or "(unknown)")
        return
    scope["priced_rows"] += 1
    scope["priced_tokens"] += tokens
    for part in AGENT_PARTS:
        scope[f"{part}_cost"] += parts.get(part, 0.0)
    scope["list_cost"] += sum(parts.values())


def _scope_stamp(scope: dict, first, last) -> None:
    for key, at, better in (("first_at", first, min), ("last_at", last, max)):
        if at is not None:
            scope[key] = at if scope[key] is None else better(scope[key], at)


def _scope_out(scope: dict, exact_window: bool) -> dict:
    denominator = scope["input_tokens"] + scope["cache_write_tokens"] + scope["cache_read_tokens"]
    return {
        **scope,
        "cache_denominator": denominator,
        "cache_hit": scope["cache_read_tokens"] / denominator if denominator else None,
        "unpriced_models": sorted(scope["unpriced_models"]),
        "exact_window": exact_window,
        "first_at": scope["first_at"].isoformat() if scope["first_at"] else None,
        "last_at": scope["last_at"].isoformat() if scope["last_at"] else None,
    }


def _merge_scopes(*scopes: dict) -> dict:
    merged = _scope()
    for scope in scopes:
        for key, value in scope.items():
            if key == "unpriced_models":
                merged[key] |= value
            elif key in ("first_at", "last_at"):
                _scope_stamp(merged, value if key == "first_at" else None, value if key == "last_at" else None)
            else:
                merged[key] += value
    return merged


def _estimate(calls: list[StructuredCall], usages: list[AgentUsage], since) -> dict:
    bot = _scope()
    for call in calls:
        counts = {part: getattr(call, field) or 0 for part, field, _ in PARTS}
        _scope_add(bot, counts, _list_parts(call), call.model_name)
        _scope_stamp(bot, call.created_at, call.created_at)
    agents = _scope()
    for usage in usages:
        counts = {part: getattr(usage, f"{part}_tokens") or 0 for part in AGENT_PARTS}
        price = price_for(usage.model)
        parts = (
            None if price is None else {part: counts[part] * getattr(price, part) / 1_000_000 for part in AGENT_PARTS}
        )
        _scope_add(agents, counts, parts, usage.model)
        _scope_stamp(agents, usage.first_at, usage.last_at)
        if usage.first_at is None or usage.first_at < since:
            agents["spillover_rows"] += 1
            agents["spillover_tokens"] += sum(counts.values())
    combined = _merge_scopes(bot, agents)
    return {
        "basis": "Model-table token estimate; aggregated totals omit per-request surcharges, billed amounts unknown",
        "bot": _scope_out(bot, True),
        "agents": _scope_out(agents, False),
        "combined": _scope_out(combined, agents["rows"] == 0),
    }


AGENT_FIELDS = tuple(f"{part}_tokens" for part in AGENT_PARTS)


def _spread(bins: list[int], since, hour: timedelta, first, last, tokens: int) -> None:
    """Add ``tokens`` spent evenly over [first, last] to the hours they overlap."""
    first = first or last
    span = (last - first).total_seconds()
    if span <= 0:
        index = int((last - since) / hour)
        if index >= 0:
            bins[min(index, len(bins) - 1)] += tokens
        return
    for index in range(len(bins)):
        start = since + index * hour
        overlap = (min(start + hour, last) - max(start, first)).total_seconds()
        if overlap > 0:
            bins[index] += round(tokens * overlap / span)


@router.get("/sessions/{session_id}/usage", response=SessionUsageOut)
def session_usage(request, session_id: str, hours: int = 6):
    """Tokens per hour for one chat over the last ``hours``, split into its own calls and its
    workers' coding agents. Agents report per-request events when their transcript was
    captured; otherwise a worker's running totals are spread evenly over its active span."""
    session = visible_sessions(request.auth).filter(id=uuid_or_404(session_id)).first()
    if session is None:
        raise HttpError(404, "No such session")
    hours = max(1, min(hours, 48))
    hour = timedelta(hours=1)
    until = timezone.now()
    since = until - hours * hour
    thread = [0] * hours
    agents = [0] * hours

    def slot(at) -> int:
        return min(hours - 1, int((at - since) / hour))

    fields = [field for _, field, _ in PARTS]
    own = _of_chats(_for_chat(StructuredCall.objects.all()), ConversationSession.objects.filter(pk=session.pk))
    for row in own.filter(created_at__gte=since, created_at__lte=until).values_list("created_at", *fields):
        thread[slot(row[0])] += sum(n or 0 for n in row[1:])

    events = AgentUsageEvent.objects.filter(
        session__thread_links__conversation=session, occurred_at__gte=since, occurred_at__lte=until
    ).distinct()
    for row in events.values_list("occurred_at", *AGENT_FIELDS):
        agents[slot(row[0])] += sum(n or 0 for n in row[1:])
    # Workers whose agent transcript was captured are counted by event above.
    captured = set(
        AgentUsageEvent.objects.filter(
            session__thread_links__conversation=session, session__thread_links__worker__isnull=False
        ).values_list("session__thread_links__worker", flat=True)
    )
    totals = AgentUsage.objects.filter(session=session, last_at__gte=since).exclude(worker__in=captured)
    for usage in totals:
        tokens = sum(getattr(usage, field) for field in AGENT_FIELDS)
        _spread(agents, since, hour, usage.first_at, min(usage.last_at, until), tokens)

    return {
        "until": until.isoformat(),
        "hours": [
            {"start": (since + index * hour).isoformat(), "thread_tokens": thread[index], "agent_tokens": agents[index]}
            for index in range(hours)
        ],
        "thread_tokens": sum(thread),
        "agent_tokens": sum(agents),
    }


@router.get("/costs", response=CostsOut)
def costs(request, days: int = 30, bot: str = ""):
    days = max(1, min(days, 366))
    until = timezone.now().astimezone(UTC)
    # Calendar-aligned UTC window: `days` UTC dates ending with today's partial day, so the
    # day series and the totals cover exactly the same instants.
    since = (until - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    calls = _for_chat(
        StructuredCall.objects.filter(created_at__gte=since, created_at__lte=until).select_related("session")
    )
    if not request.auth.is_superuser:
        calls = calls.filter(user=request.auth)
    if bot:
        calls = _of_chats(calls, ConversationSession.objects.filter(bot_name=bot))
    calls = list(calls.order_by("created_at"))
    chats = _chat_of(calls)
    agent_rows = AgentUsage.objects.filter(last_at__gte=since, last_at__lte=until).select_related("worker", "session")
    if not request.auth.is_superuser:
        agent_rows = agent_rows.filter(session__user=request.auth)
    if bot:
        agent_rows = agent_rows.filter(bot_name=bot)
    private = InferenceRequest.objects.filter(created_at__gte=since, created_at__lte=until)
    if not request.auth.is_superuser:
        private = private.filter(user=request.auth)
    if bot:
        private = private.filter(bot_name=bot)
    total = _empty("total")
    by_kind: dict[str, dict] = {}
    by_bot: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_day: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    unpriced: set[str] = set()
    by_session: dict[str, dict] = {}
    main_tokens = subscription_tokens = compaction_tokens = 0
    for call in calls:
        parts = call_costs(call)
        cost = None if parts is None else sum(parts.values())
        if parts is None:
            unpriced.add(call.model_name or "(unknown)")
        _add(total, call, parts)
        main_count, subscription_count = _add_session(by_session, call, parts, chats.get(call.for_chat or ""))
        main_tokens += main_count
        subscription_tokens += subscription_count
        if call.kind == "compaction":
            compaction_tokens += sum((getattr(call, field) or 0) for _, field, _ in PARTS)
        _add(by_kind.setdefault(call.kind, _empty(call.kind)), call, parts)
        model = call.model_name or "(unknown)"
        _add(by_model.setdefault(model, _empty(model)), call, parts)
        if call.kind == CHAT_REPLY_KIND:
            chat_bot = (call.session.bot_name if call.session else "") or "(no bot)"
            _add(by_bot.setdefault(chat_bot, _empty(chat_bot)), call, parts)
        day = by_day[call.created_at.astimezone(UTC).date().isoformat()]
        day["calls"] += 1
        day["cost"] += cost or 0.0

    first_day = since.date()
    series = [
        {"date": d, **by_day.get(d, {"cost": 0.0, "calls": 0})}
        for d in ((first_day + timedelta(days=n)).isoformat() for n in range(days))
    ]
    agent_list = list(agent_rows)
    return {
        "days": days,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "timezone": "UTC",
        "total": total,
        "by_kind": _sorted(by_kind),
        "chat_reply_by_bot": _sorted(by_bot),
        "by_model": _sorted(by_model),
        "by_day": series,
        "unpriced_models": sorted(unpriced),
        "agents": _agents(agent_list, since),
        "usage": _usage(total, by_session, main_tokens, subscription_tokens, compaction_tokens),
        "private": _private_spend(private.order_by("created_at")),
        "estimate": _estimate(calls, agent_list, since),
    }
