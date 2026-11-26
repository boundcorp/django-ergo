"""A read-only portfolio/status snapshot for orchestrators: ``ergo_portfolio_status``.

One call answers "what is going on across my bots, and what needs follow-through?"
without the per-thread reads (``thread_status``, history) or GitHub calls the
"Bots and threads" block makes. Everything comes from rows already stored, in a
fixed number of queries however many chats there are:

- ``threads``: the caller's main chats, named chats and open threads, across the
  bots it may message (the same scope as ``bots.overview``), with recorded state,
  a stored summary and the latest substantive reply (text and time);
- ``resolved``: threads resolved within the lookback window, for follow-through;
- ``workers``: owner chat, status and last activity of recent and running workers;
- ``pull_requests``: the stored PR inventory (the ``github_pr`` files that
  ``conversation.links`` records), with the check state and when it was stored;
- ``attachments``: recent file metadata, with links only for files the caller
  may read.

It is a **stored snapshot**. Nothing here calls GitHub, another bot or a model.
States that aren't stored are said so ("unknown", "not stored"); blockers are
only what is recorded (a pending approval, an open request, a question to the
user, a waiting reply), never inferred. Each source reports its own freshness,
and a source that fails is named in ``errors`` while the others still return.

Access follows the caller: only the caller's own user's chats, only bots the
caller bot may message (private bots are never in scope), and files of other
chats only when the attachments plugin allows other sessions. A private-export
copy is visible only in the chat it was exported to. Nothing outside that scope
is counted, named or correlated (a PR is shown with the chats that recorded it
within scope, a worker only under a chat in scope).
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.db.models import OuterRef
from django.db.models import Q
from django.db.models import Subquery
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce
from django.db.models.functions import Greatest
from django.db.models.functions import Left
from django.utils import timezone

from django_ergo.bots import messaging
from django_ergo.bots.registry import may_use
from django_ergo.conversation import roles
from django_ergo.conversation.links import GITHUB_PR
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationCompaction
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import ThreadMessageStatus
from django_ergo.conversation.models import Worker
from django_ergo.conversation.models import WorkerStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.tools import ToolContext

logger = logging.getLogger(__name__)

SOURCES = ("threads", "resolved", "workers", "pull_requests", "attachments")
DEFAULT_LOOKBACK_HOURS = 72
MAX_LOOKBACK_HOURS = 24 * 30
LIMITS = {  # source: (default, maximum)
    "threads": (30, 100),
    "resolved": (10, 30),
    "workers": (20, 50),
    "pull_requests": (20, 50),
    "attachments": (15, 40),
}
SUMMARY_CHARS = 300
EVIDENCE_CHARS = 500
PR_CHATS = 3  # chats named per pull request
PR_SCAN = 200  # stored PR rows read to build the (deduplicated) inventory
OPEN = [
    ThreadMessageStatus.QUEUED,
    ThreadMessageStatus.DELIVERED,
    ThreadMessageStatus.WAITING,
]
ACTIVE_WORKERS = [WorkerStatus.QUEUED, WorkerStatus.RUNNING]
# Relative to the Ergonaut host that serves the app and API.
SESSION_LINK = "/s/{id}"
ATTACHMENT_LINK = "/api/attachments/{id}/download"
LABEL_FRESHNESS = (
    "stored snapshot: read from the database at generated_at; GitHub, other bots "
    "and workers were not contacted. Pull request check states are as last stored "
    "(checked_at); review state is not stored."
)


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _iso(when) -> str | None:
    return when.isoformat(timespec="seconds") if when else None


def _limit(name: str, value) -> int:
    default, maximum = LIMITS[name]
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def scope_bots(ctx: ToolContext, bot: str = "") -> list[Bot]:
    """Bots in scope: this bot and the bots it may message and the user may use
    (as ``bots.overview``). ``bot`` narrows to one of them; a name that isn't in
    scope gets the same answer whether or not such a bot exists."""
    caller = ctx.bot
    bots = [
        caller,
        *(
            [
                b
                for b in caller.registry.callable_bots(caller)
                if may_use(b, ctx.session.user)
            ]
            if caller.registry
            else []
        ),
    ]
    if bot:
        bots = [b for b in bots if b.name == bot]
        if not bots:
            msg = f"No bot {bot!r} you can see a status for."
            raise ValueError(msg)
    return bots


def chat_kind(session: ConversationSession) -> str:
    return {roles.MAIN: "main", roles.CHAT: "named_chat"}.get(
        roles.role(session), "thread"
    )


def session_ref(session: ConversationSession) -> dict:
    return {
        "session_id": str(session.id),
        "bot": session.bot_name,
        "kind": chat_kind(session),
        "title": messaging.label(session),
        "link": SESSION_LINK.format(id=session.id),
    }


def _sessions(ctx: ToolContext, names: list[str]):
    from django_ergo.bots.registry import unreadable

    # Sessions under a privacy policy this work isn't under (bots.pools) are
    # out of scope: not counted, named or summarized.
    return ConversationSession.objects.filter(
        user_id=ctx.session.user_id, bot_name__in=names
    ).exclude(unreadable(ctx.bot))


def _annotated(qs):
    """Latest reply, compaction summary and last activity of each session, as
    subqueries of the one sessions query (no per-session reads)."""
    replies = (
        StructuredCall.objects.filter(
            session=OuterRef("pk"),
            kind="chat_reply",
            status=StructuredCallStatus.COMPLETED,
        )
        .exclude(Q(response__text__isnull=True) | Q(response__text=""))
        .order_by("-updated_at", "-id")
    )
    calls = StructuredCall.objects.filter(session=OuterRef("pk")).order_by(
        "-updated_at"
    )
    summaries = ConversationCompaction.objects.filter(session=OuterRef("pk")).order_by(
        "-upto_sequence"
    )
    return qs.annotate(
        reply_text=Subquery(
            replies.annotate(t=Left(KeyTextTransform("text", "response"), 2000)).values(
                "t"
            )[:1]
        ),
        reply_type=Subquery(
            replies.annotate(t=KeyTextTransform("type", "response")).values("t")[:1]
        ),
        reply_at=Subquery(replies.values("updated_at")[:1]),
        compaction=Subquery(
            summaries.annotate(t=Left("summary", 1200)).values("t")[:1]
        ),
        compaction_at=Subquery(summaries.values("created_at")[:1]),
        activity=Greatest(
            Coalesce(Subquery(calls.values("updated_at")[:1]), "updated_at"),
            "updated_at",
        ),
    )


def _per_session(model_qs, key: str) -> dict:
    return {row[key]: row["n"] for row in model_qs.values(key).annotate(n=Count("id"))}


def _signals(ids: list) -> dict:
    """Recorded state of ``ids``: approvals, open requests in and out, running
    workers and who a chat waits on that is itself paused for approval."""
    calls = StructuredCall.objects.filter(session_id__in=ids)
    approval = set(
        calls.filter(messaging.awaiting_approval()).values_list("session_id", flat=True)
    )
    running = set(
        calls.filter(messaging.running_turn()).values_list("session_id", flat=True)
    )
    inbound = _per_session(
        ThreadMessage.objects.filter(
            recipient_session_id__in=ids, status__in=OPEN, in_reply_to__isnull=True
        ),
        "recipient_session_id",
    )
    outbound = _per_session(
        ThreadMessage.objects.filter(
            sender_session_id__in=ids, status__in=OPEN, in_reply_to__isnull=True
        ).filter(~Q(metadata__report=True) | Q(metadata__report__isnull=True)),
        "sender_session_id",
    )
    workers = _per_session(
        Worker.objects.filter(session_id__in=ids, status__in=ACTIVE_WORKERS).exclude(
            function="browser:session"
        ),
        "session_id",
    )
    return {
        "approval": approval,
        "running": running,
        "inbound": inbound,
        "outbound": outbound,
        "workers": workers,
    }


def _blocked_on(ids: list, names: list[str], user_id) -> dict:
    """session id -> labels of in-scope chats it waits on that are paused for the
    user's approval (a recorded fact; chats out of scope are never named)."""
    found: dict = {}
    waiting = (
        ThreadMessage.objects.filter(
            sender_session_id__in=ids,
            status=ThreadMessageStatus.WAITING,
            in_reply_to__isnull=True,
            recipient_session__user_id=user_id,
            recipient_session__bot_name__in=names,
            recipient_session__structured_calls__status=StructuredCallStatus.AWAITING_APPROVAL,
        )
        .filter(~Q(metadata__report=True) | Q(metadata__report__isnull=True))
        .select_related("recipient_session")
        .distinct()
    )
    for message in waiting:
        found.setdefault(message.sender_session_id, set()).add(
            messaging.label(message.recipient_session)
        )
    return {k: sorted(v) for k, v in found.items()}


def _thread_row(session, current, signals: dict, blocked: dict) -> dict:
    sid = session.id
    meta = session.metadata or {}
    if sid in signals["approval"]:
        state = "waiting_for_approval"
    elif sid in signals["running"]:
        state = "working"
    elif signals["workers"].get(sid, 0):
        state = "waiting_on_worker"
    else:
        state = "idle"
    row = {
        **session_ref(session),
        "state": state,
        "last_activity": _iso(session.activity),
        "recorded": {
            "open_requests_in": signals["inbound"].get(sid, 0),
            "open_requests_out": signals["outbound"].get(sid, 0),
            "workers_running": signals["workers"].get(sid, 0),
            "asks_user": session.reply_type == "question",
            "pending_approval": state == "waiting_for_approval",
            "waiting_on_approval_of": blocked.get(sid, []),
        },
        "summary": _clip(session.compaction, SUMMARY_CHARS) or None,
        "summary_at": _iso(session.compaction_at),
        "latest_reply": (
            {
                "type": session.reply_type or "message",
                "text": _clip(session.reply_text, EVIDENCE_CHARS),
                "at": _iso(session.reply_at),
            }
            if session.reply_text
            else None
        ),
    }
    if meta.get("started_by_label"):
        row["started_by"] = str(meta["started_by_label"])
    if sid == current.id:
        row["you_are_here"] = True
    return row


def _threads(ctx, names, *, limit, offset, **_) -> tuple[list, dict]:
    qs = _sessions(ctx, names).filter(
        ~Q(status="completed") | Q(metadata__bot_role__in=list(roles.TOP))
    )
    total = qs.count()
    page = list(_annotated(qs).order_by("-activity", "id")[offset : offset + limit])
    ids = [s.id for s in page]
    signals = _signals(ids) if ids else None
    blocked = _blocked_on(ids, names, ctx.session.user_id) if ids else {}
    rows = [_thread_row(s, ctx.session, signals, blocked) for s in page]
    meta = {
        "total": total,
        "newest": max((r["last_activity"] for r in rows), default=None),
    }
    return rows, {**meta, "offset": offset, "more": offset + len(rows) < total}


def _resolved(ctx, names, *, limit, cutoff, **_) -> tuple[list, dict]:
    qs = (
        _sessions(ctx, names)
        .filter(status="completed", updated_at__gte=cutoff)
        .exclude(metadata__bot_role__in=list(roles.TOP))
    )
    total = qs.count()
    rows = []
    for session in _annotated(qs).order_by("-updated_at", "id")[:limit]:
        meta = session.metadata or {}
        rows.append(
            {
                **session_ref(session),
                "resolved_at": meta.get("archived_at") or _iso(session.updated_at),
                "resolved_by": meta.get("resolved_by"),
                "resolved_summary": _clip(meta.get("resolved_summary"), SUMMARY_CHARS)
                or None,
                "latest_reply": (
                    {
                        "text": _clip(session.reply_text, EVIDENCE_CHARS),
                        "at": _iso(session.reply_at),
                    }
                    if session.reply_text
                    else None
                ),
            }
        )
    return rows, {
        "total": total,
        "newest": max((r["resolved_at"] for r in rows), default=None),
        "more": total > len(rows),
    }


def _worker_activity(worker) -> datetime:
    at = ((worker.state or {}).get("activity") or {}).get("at")
    stamps = [worker.updated_at, worker.started_at, worker.completed_at]
    if isinstance(at, int | float):
        when = datetime.fromtimestamp(at, tz=UTC)
        stamps.append(when if settings.USE_TZ else when.replace(tzinfo=None))
    return max(s for s in stamps if s)


def _workers(ctx, names, *, limit, cutoff, **_) -> tuple[list, dict]:
    from django_ergo.bots.registry import unreadable

    qs = (
        Worker.objects.filter(
            session__user_id=ctx.session.user_id, session__bot_name__in=names
        )
        .exclude(unreadable(ctx.bot, "session__"))
        .filter(Q(status__in=ACTIVE_WORKERS) | Q(updated_at__gte=cutoff))
    )
    total = qs.count()
    from django.db.models import Case
    from django.db.models import IntegerField
    from django.db.models import Value
    from django.db.models import When

    page = list(
        qs.select_related("session")
        .annotate(
            priority=Case(
                When(status__in=ACTIVE_WORKERS, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("priority", "-updated_at", "id")[:limit]
    )
    rows = [
        {
            "id": str(w.id),
            "owner": session_ref(w.session),
            "title": _clip(w.title, 120),
            "kind": w.function.partition(":")[0],
            "status": w.status,
            "progress": _clip(w.progress, 200) or None,
            "has_error": bool(w.error),
            "started_at": _iso(w.started_at),
            "completed_at": _iso(w.completed_at),
            "last_activity": _iso(_worker_activity(w)),
        }
        for w in page
    ]
    rows.sort(key=lambda r: r["last_activity"], reverse=True)
    rows.sort(key=lambda r: r["status"] not in ACTIVE_WORKERS)
    return rows, {
        "total": total,
        "newest": max((r["last_activity"] for r in rows), default=None),
        "more": total > len(rows),
    }


def _visible_files(ctx: ToolContext, names: list[str]):
    """File rows of in-scope chats the caller may see: another chat's files only
    when the attachments plugin allows other sessions, and a private-export copy
    only in the chat it was exported to (as ``AttachmentsPlugin.file_for``)."""
    plugin = ctx.bot.plugin("attachments")
    from django_ergo.bots.registry import unreadable

    qs = ConversationAttachment.objects.filter(
        session__user_id=ctx.session.user_id, session__bot_name__in=names
    ).exclude(unreadable(ctx.bot, "session__"))
    own = Q(session_id=ctx.session.id)
    others = (
        Q(private_export__isnull=True)
        if plugin is not None and getattr(plugin, "other_sessions", False)
        else Q(pk__in=[])
    )
    return qs.filter(own | others)


def _pull_requests(ctx, names, *, limit, cutoff, **_) -> tuple[list, dict]:
    rows = (
        _visible_files(ctx, names)
        .filter(metadata__link=GITHUB_PR)
        .filter(
            Q(metadata__state__isnull=True)
            | ~Q(metadata__state__in=["merged", "closed"])
            | Q(updated_at__gte=cutoff)
        )
        .select_related("session")
        .order_by("-updated_at", "id")[:PR_SCAN]
    )
    by_url: dict[str, dict] = {}
    for row in rows:
        meta = row.metadata or {}
        entry = by_url.get(row.url)
        if entry is None:
            state = str(meta.get("state") or "")
            checks = str(meta.get("checks") or "")
            checked_at = meta.get("checked_at")
            entry = by_url[row.url] = {
                "url": row.url,
                "repo": meta.get("repo") or "",
                "number": meta.get("number"),
                "title": _clip(meta.get("title"), 160) or None,
                "state": state or "unknown",
                "checks": checks or ("unknown" if checked_at else "not stored"),
                "checks_stored_at": checked_at or "not stored",
                "review": "not stored",
                "mergeable": "not stored",
                "recorded_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
                "chats": [],
            }
        if len(entry["chats"]) < PR_CHATS:
            entry["chats"].append(session_ref(row.session))
    found = list(by_url.values())
    shown = found[:limit]
    stamps = [
        p["checks_stored_at"] for p in shown if p["checks_stored_at"] != "not stored"
    ]
    return shown, {
        "total": len(found),
        "more": len(found) > len(shown) or len(rows) >= PR_SCAN,
        "newest": max(stamps, default=None),
        "oldest": min(stamps, default=None),
        "never_refreshed": sum(
            1 for p in shown if p["checks_stored_at"] == "not stored"
        ),
    }


def _attachments(ctx, names, *, limit, cutoff, **_) -> tuple[list, dict]:
    qs = (
        _visible_files(ctx, names)
        .filter(Q(metadata__link__isnull=True) | ~Q(metadata__link=GITHUB_PR))
        .filter(archived_at__isnull=True, updated_at__gte=cutoff)
    )
    total = qs.count()
    rows = [
        {
            "id": str(a.id),
            "filename": a.filename,
            "media_type": a.media_type,
            "size": a.size,
            "source": a.source,
            "updated_at": _iso(a.updated_at),
            "chat": session_ref(a.session),
            "link": ATTACHMENT_LINK.format(id=a.id) if a.file else None,
        }
        for a in qs.select_related("session").order_by("-updated_at", "id")[:limit]
    ]
    return rows, {
        "total": total,
        "newest": max((r["updated_at"] for r in rows), default=None),
        "more": total > len(rows),
    }


READERS: dict[str, Callable] = {
    "threads": _threads,
    "resolved": _resolved,
    "workers": _workers,
    "pull_requests": _pull_requests,
    "attachments": _attachments,
}


def portfolio_status(  # noqa: PLR0913
    ctx: ToolContext,
    *,
    bot: str = "",
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    limit: int = 0,
    offset: int = 0,
    resolved_limit: int = 0,
    sources: list[str] | None = None,
) -> dict:
    """The stored snapshot (see the module docstring). ``bot`` narrows the scope,
    ``lookback_hours`` bounds resolved work, workers, files and closed PRs, and
    ``limit``/``offset`` page the active threads. A failing source is reported in
    ``errors`` and the rest still return."""
    now = timezone.now()
    try:
        hours = max(1, min(int(lookback_hours), MAX_LOOKBACK_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_LOOKBACK_HOURS
    bots = scope_bots(ctx, bot)
    names = [b.name for b in bots]
    wanted = [s for s in SOURCES if not sources or s in sources]
    cutoff = now - timedelta(hours=hours)
    common = {"cutoff": cutoff, "offset": max(int(offset or 0), 0)}
    result: dict = {
        "snapshot": {
            "mode": "stored_snapshot",
            "generated_at": _iso(now),
            "live_refresh": "not performed",
            "note": LABEL_FRESHNESS,
            "lookback_hours": hours,
            "lookback_from": _iso(cutoff),
        },
        "scope": {"bots": names, "bot_filter": bot or None, "sources": wanted},
    }
    coverage, errors = {}, {}
    for source in SOURCES:
        if source not in wanted:
            continue
        asked = {"threads": limit, "resolved": resolved_limit}.get(source, 0)
        size = _limit(source, asked or LIMITS[source][0])
        try:
            with transaction.atomic():  # a failed source leaves the others usable
                rows, info = READERS[source](ctx, names, limit=size, **common)
        except Exception as exc:  # noqa: BLE001 - partial results beat none
            logger.warning("Portfolio status source %s failed", source, exc_info=True)
            result[source] = []
            errors[source] = f"{source} unavailable ({type(exc).__name__})"
            coverage[source] = {"status": "error", "returned": 0}
            continue
        result[source] = rows
        coverage[source] = {
            "status": "ok",
            "returned": len(rows),
            "limit": size,
            "truncated": bool(info.pop("more", False)),
            "freshness": {
                "read_at": _iso(now),
                **{k: v for k, v in info.items() if k in ("newest", "oldest")},
            },
            **{k: v for k, v in info.items() if k not in ("newest", "oldest", "more")},
        }
    threads = coverage.get("threads")
    if threads and threads.get("status") == "ok" and threads["truncated"]:
        threads["next_offset"] = common["offset"] + threads["returned"]
    result["coverage"] = coverage
    result["errors"] = errors
    result["partial"] = bool(errors)
    return result
