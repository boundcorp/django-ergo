"""Deploy watches: tell a chat once the code it asked for is actually running.

A chat that asked for a fix used to poll with self-wakes until the fix was
deployed. Instead it calls ``ergo_notify_when_live`` with the commit, and gets
one automated event when every role of the host runs that commit, or one "not
live after N hours" event when the watch expires.

**What counts as live.** Only what the running processes report about
themselves. Each long-running process of the host (Ergonaut: web, the Celery
workers, beat and the bots runner) calls :func:`report` with the revision baked
into its own build and whether it has finished starting, and refreshes the
report on an interval (``seen_at``). A revision is live when, for every role in
``DJANGO_ERGO["LIVE_ROLES"]``:

- at least one process of that role has reported within
  ``LIVE_REPORT_STALE_SECONDS``,
- every such process is ready, and
- every such process runs the revision, or a commit that contains it (an
  ancestry check through ``REVISION_ANCESTRY``; without one, only an exact
  match counts).

A merged pull request, a pushed image or a deployment manifest is never
evidence: an old pod that is still running keeps the revision from being live
until it stops, and a role nobody reports for is not live. A process that
dies without withdrawing its report stops counting once its report is stale.

**Delivery** follows the self-wake rules (``bots.wakes``): a watch is a
``LiveWatch`` row, unique per (chat, revision). :func:`check_due` (Ergonaut's
beat runs it every 30 seconds) claims a pending row with a conditional UPDATE
in the same transaction that creates the chat's event message, so concurrent
checkers, a double tick or a restart send it once, and a crash before the
commit sends nothing. A failed send is final, like a wake's. A watch whose
``expires_at`` passes is closed with an explicit "not live" event naming what
was still missing, never silently.

**Scope.** A watch belongs to the chat whose turn created it: the tools take
no chat id, and list and cancel only that chat's watches.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone

from django_ergo.conversation.budget import forbid
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import LiveWatch
from django_ergo.conversation.models import LiveWatchStatus
from django_ergo.conversation.models import RevisionAncestry
from django_ergo.conversation.models import RuntimeReport
from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from django_ergo.bots.registry import BotRegistry
    from django_ergo.bots.skillset import SkillDef
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import ToolContext

logger = logging.getLogger(__name__)

SOURCE = "live_watch"
SOURCE_LABEL = "Deploy watch"
MAX_PENDING_PER_CHAT = 5
MAX_NOTE_CHARS = 2000
CHECK_BATCH = 200
# Reports nobody refreshed for this long are deleted by ``prune``.
PRUNE_AFTER = timedelta(days=1)
# An ancestry check that couldn't answer isn't asked again for this long.
UNKNOWN_RETRY_SECONDS = 600
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


# -- settings ----------------------------------------------------------------


def required_roles() -> list[str]:
    """The roles that must all run a revision for it to be live; empty means
    the host doesn't report its processes, and watches are off."""
    roles = api_settings.LIVE_ROLES or []
    if isinstance(roles, str):
        roles = roles.split(",")
    return [r.strip() for r in roles if r and r.strip()]


def enabled() -> bool:
    return bool(required_roles())


def stale_after() -> timedelta:
    return timedelta(seconds=float(api_settings.LIVE_REPORT_STALE_SECONDS))


# -- reports from the running processes --------------------------------------


def report(
    instance: str,
    roles: list[str] | tuple[str, ...],
    revision: str,
    *,
    ready: bool,
    now: datetime | None = None,
) -> None:
    """Record what one running process runs, for each role it serves.

    Call it from the process itself, with the revision baked into its build,
    at start (``ready=False`` until it serves) and on an interval after."""
    now = now or timezone.now()
    revision = (revision or "").strip().lower()
    for role in roles:
        fields = {"revision": revision, "ready": ready, "seen_at": now}
        if RuntimeReport.objects.filter(instance=instance, role=role).update(**fields):
            continue
        try:
            with transaction.atomic():
                RuntimeReport.objects.create(
                    instance=instance, role=role, started_at=now, **fields
                )
        except IntegrityError:  # the same instance raced itself; keep the newest
            RuntimeReport.objects.filter(instance=instance, role=role).update(**fields)


def withdraw(instance: str) -> int:
    """A process is stopping cleanly: its reports stop counting at once."""
    deleted, _ = RuntimeReport.objects.filter(instance=instance).delete()
    return deleted


def prune(now: datetime | None = None) -> int:
    now = now or timezone.now()
    deleted, _ = RuntimeReport.objects.filter(seen_at__lt=now - PRUNE_AFTER).delete()
    return deleted


def running(now: datetime | None = None) -> dict[str, list[dict]]:
    """The fresh reports, by role: what is running now."""
    now = now or timezone.now()
    out: dict[str, list[dict]] = {}
    for r in RuntimeReport.objects.filter(seen_at__gte=now - stale_after()):
        out.setdefault(r.role, []).append(
            {
                "instance": r.instance,
                "revision": r.revision,
                "ready": r.ready,
                "seen_at": r.seen_at.isoformat(timespec="seconds"),
            }
        )
    return out


# -- revisions ---------------------------------------------------------------


def resolve(requested: str) -> str:
    """The full revision a watch waits for. ``REVISION_RESOLVER`` may expand
    short SHAs or image digests; without it only a full 40-character commit
    SHA is accepted. ValueError (for the model) when it can't be resolved."""
    text = (requested or "").strip()
    if not text:
        msg = "Give the commit SHA (or image digest) to wait for"
        raise ValueError(msg)
    resolver = api_settings.REVISION_RESOLVER
    if resolver is not None:
        revision = resolver(text)
    else:
        revision = text.lower()
        if not FULL_SHA.match(revision):
            msg = (
                f"{text!r} is not a full 40-character commit SHA, and this host "
                "can't resolve short SHAs or image digests: give the full commit"
            )
            raise ValueError(msg)
    revision = (revision or "").strip().lower()
    if not revision:
        msg = f"Couldn't resolve {text!r} to a commit"
        raise ValueError(msg)
    return revision


_unknown: dict[tuple[str, str], float] = {}


def contains(target: str, revision: str) -> bool:  # noqa: PLR0911
    """Whether ``revision`` is ``target`` or a descendant of it. Asks
    ``REVISION_ANCESTRY`` (cached in the database for definite answers);
    without it, or when it can't tell, only an exact match counts."""
    if not revision:
        return False
    if revision == target:
        return True
    cached = (
        RevisionAncestry.objects.filter(target=target, revision=revision)
        .values_list("contains", flat=True)
        .first()
    )
    if cached is not None:
        return cached
    check = api_settings.REVISION_ANCESTRY
    if check is None:
        return False
    key = (target, revision)
    if (
        time.monotonic() - _unknown.get(key, -UNKNOWN_RETRY_SECONDS)
        < UNKNOWN_RETRY_SECONDS
    ):
        return False
    try:
        answer = check(target, revision)
    except Exception:  # noqa: BLE001 — the host's hook; an error means "can't tell"
        logger.warning(
            "Ancestry check %s..%s failed", target[:12], revision[:12], exc_info=True
        )
        answer = None
    if answer is None:
        _unknown[key] = time.monotonic()
        return False
    RevisionAncestry.objects.get_or_create(
        target=target, revision=revision, defaults={"contains": bool(answer)}
    )
    return bool(answer)


# -- the verdict -------------------------------------------------------------


@dataclass
class Verdict:
    """Whether ``revision`` is live, and per role why (not)."""

    revision: str
    live: bool
    checked_at: datetime
    roles: dict[str, dict] = field(default_factory=dict)

    def missing(self) -> list[str]:
        return [f"{role}: {r['why']}" for role, r in self.roles.items() if not r["ok"]]

    def evidence(self) -> dict:
        return {
            "revision": self.revision,
            "live": self.live,
            "checked_at": self.checked_at.isoformat(timespec="seconds"),
            "roles": self.roles,
        }


def verdict(revision: str, now: datetime | None = None) -> Verdict:
    """Is ``revision`` running, ready, in every required role? From the
    processes' own fresh reports only."""
    now = now or timezone.now()
    fresh = running(now)
    roles: dict[str, dict] = {}
    for role in required_roles():
        reports = fresh.get(role, [])
        instances = [
            {
                "instance": r["instance"],
                "revision": r["revision"],
                "ready": r["ready"],
                "contains": contains(revision, r["revision"]),
            }
            for r in reports
        ]
        if not instances:
            why = "no running process has reported"
        elif behind := [i for i in instances if not i["contains"]]:
            why = ", ".join(
                f"{i['instance']} runs {i['revision'][:12] or 'an unknown revision'}"
                for i in behind
            )
        elif starting := [i for i in instances if not i["ready"]]:
            why = ", ".join(f"{i['instance']} is still starting" for i in starting)
        else:
            why = ""
        roles[role] = {"ok": not why, "why": why, "instances": instances}
    live = bool(roles) and all(r["ok"] for r in roles.values())
    return Verdict(revision=revision, live=live, checked_at=now, roles=roles)


# -- watches -----------------------------------------------------------------


def watch(
    session: ConversationSession,
    requested: str,
    *,
    note: str = "",
    hours: float | None = None,
    now: datetime | None = None,
) -> tuple[LiveWatch, bool]:
    """Arm this chat's watch for a revision. Returns (watch, created); a watch
    already pending for the same revision is returned as it is."""
    forbid("watch for a deploy")
    if not enabled():
        msg = (
            "This server doesn't report which revision its processes run, so it "
            "can't tell when a change is live (DJANGO_ERGO LIVE_ROLES is empty)."
        )
        raise ValueError(msg)
    now = now or timezone.now()
    revision = resolve(requested)
    default_hours = float(api_settings.LIVE_WATCH_HOURS)
    max_hours = float(api_settings.LIVE_WATCH_MAX_HOURS)
    try:
        hours = default_hours if hours is None else float(hours)
    except (TypeError, ValueError):
        msg = "hours must be a number"
        raise ValueError(msg) from None
    if not 0 < hours <= max_hours:
        msg = f"hours must be more than 0 and at most {max_hours:g}"
        raise ValueError(msg)
    expires = now + timedelta(hours=hours)
    note = (note or "").strip()[:MAX_NOTE_CHARS]
    from django_ergo.bots.messaging import current_thread_message

    handling = current_thread_message(session)
    depth = handling.depth if handling else 0
    with transaction.atomic():
        existing = (
            LiveWatch.objects.select_for_update()
            .filter(session=session, revision=revision)
            .first()
        )
        if existing is not None and existing.status == LiveWatchStatus.PENDING:
            return existing, False
        if existing is not None and existing.status == LiveWatchStatus.LIVE:
            when = existing.settled_at.isoformat(timespec="minutes")
            msg = (
                f"This chat was already told {revision[:12]} is live ({when}); "
                "a chat hears about a revision once."
            )
            raise ValueError(msg)
        pending = LiveWatch.objects.filter(
            session=session, status=LiveWatchStatus.PENDING
        ).count()
        if pending >= MAX_PENDING_PER_CHAT:
            msg = (
                f"This chat already waits for {pending} revisions; cancel one with "
                "ergo_live_watch_cancel first."
            )
            raise ValueError(msg)
        fields = {
            "status": LiveWatchStatus.PENDING,
            "requested": requested.strip()[:200],
            "note": note,
            "expires_at": expires,
            "depth": depth,
            "settled_at": None,
            "evidence": {},
            "thread_message": None,
            "error": "",
        }
        if existing is not None:
            # Cancelled, expired or failed: the chat asks again, so re-arm it.
            for name, value in fields.items():
                setattr(existing, name, value)
            existing.save()
            armed = existing
        else:
            try:
                with transaction.atomic():
                    armed = LiveWatch.objects.create(
                        session=session,
                        bot_name=session.bot_name,
                        revision=revision,
                        **fields,
                    )
            except IntegrityError:  # another call armed it first
                return LiveWatch.objects.get(session=session, revision=revision), False
    _notify(session)
    return armed, True


def cancel(session: ConversationSession, watch_id: str) -> bool:
    """Cancel one of this chat's pending watches. A watch of another chat is
    treated as not found."""
    try:
        watch_id = uuid.UUID(str(watch_id))
    except ValueError:
        return False
    cancelled = LiveWatch.objects.filter(
        id=watch_id, session=session, status=LiveWatchStatus.PENDING
    ).update(status=LiveWatchStatus.CANCELLED, updated_at=timezone.now())
    if cancelled:
        _notify(session)
    return bool(cancelled)


def describe(w: LiveWatch) -> dict:
    return {
        "id": str(w.id),
        "revision": w.revision,
        "requested": w.requested,
        "status": w.status,
        "note": w.note,
        "expires_at": w.expires_at.isoformat(timespec="minutes"),
        "settled_at": w.settled_at.isoformat(timespec="minutes")
        if w.settled_at
        else None,
    }


def watches(session: ConversationSession, *, limit: int = 20) -> list[dict]:
    return [describe(w) for w in LiveWatch.objects.filter(session=session)[:limit]]


# -- checking and delivery ----------------------------------------------------


def check_due(
    now: datetime | None = None, registry: BotRegistry | None = None
) -> list[str]:
    """Settle every pending watch whose revision is live or whose time is up.
    Returns the ids settled here (one another checker settled is skipped)."""
    now = now or timezone.now()
    pending = list(
        LiveWatch.objects.filter(status=LiveWatchStatus.PENDING)
        .order_by("created_at")
        .values_list("id", "revision", "expires_at")[:CHECK_BATCH]
    )
    verdicts: dict[str, Verdict] = {}
    settled = []
    for watch_id, revision, expires_at in pending:
        if revision not in verdicts:
            verdicts[revision] = verdict(revision, now)
        v = verdicts[revision]
        if v.live:
            outcome = LiveWatchStatus.LIVE
        elif expires_at <= now:
            outcome = LiveWatchStatus.EXPIRED
        else:
            continue
        if settle(watch_id, outcome, v, now=now, registry=registry) is not None:
            settled.append(str(watch_id))
    return settled


def settle(
    watch_id,
    outcome: str,
    v: Verdict,
    *,
    now: datetime | None = None,
    registry: BotRegistry | None = None,
) -> LiveWatch | None:
    """Send one watch's event, once. None when it isn't pending any more."""
    now = now or timezone.now()
    with transaction.atomic():
        # The claim: a concurrent checker waits on this row's lock, then matches nothing.
        if not LiveWatch.objects.filter(
            id=watch_id, status=LiveWatchStatus.PENDING
        ).update(status=outcome, settled_at=now, evidence=v.evidence(), updated_at=now):
            return None
        w = LiveWatch.objects.select_related("session__user").get(id=watch_id)
        try:
            with transaction.atomic():
                w.thread_message = _send(w, v, registry)
        except Exception as exc:
            logger.exception("Sending deploy watch %s failed", watch_id)
            w.status = LiveWatchStatus.FAILED
            w.error = str(exc) or type(exc).__name__
        w.save(update_fields=["status", "thread_message", "error", "updated_at"])
    _notify(w.session)
    return w


def _send(w: LiveWatch, v: Verdict, registry):
    from django_ergo.bots import messaging

    return messaging.send_event(
        w.session,
        event_text(w, v),
        source=SOURCE,
        source_label=SOURCE_LABEL,
        registry=registry,
        metadata={"live_watch": str(w.id), "live_watch_status": w.status},
        depth=w.depth,
    )


def event_text(w: LiveWatch, v: Verdict) -> str:
    short = w.revision[:12]
    asked = (
        f" (you asked for {w.requested})" if w.requested.lower() != w.revision else ""
    )
    lines = []
    for role, r in v.roles.items():
        revisions = sorted({i["revision"][:12] for i in r["instances"]}) or ["-"]
        state = "ok" if r["ok"] else r["why"]
        lines.append(f"- {role}: {', '.join(revisions)} ({state})")
    roles = "\n".join(lines)
    note = f"\n\nYour note:\n{w.note}" if w.note else ""
    if w.status == LiveWatchStatus.LIVE:
        return (
            f"[Deploy watch: {short}{asked} is live. Automated notification, not "
            "a message from the user.]\n"
            f"Every required role has a running, ready process that reports "
            f"{short} or a newer commit containing it, from the processes' own "
            f"reports at {v.checked_at:%Y-%m-%d %H:%M %Z}:\n{roles}{note}"
        )
    hours = (w.expires_at - w.created_at).total_seconds() / 3600
    return (
        f"[Deploy watch: {short}{asked} is NOT live after {hours:.1f} hours. "
        "Automated notification, not a message from the user.]\n"
        "The watch is closed. Still missing:\n"
        + "\n".join(f"- {m}" for m in v.missing())
        + f"\nAll roles at {v.checked_at:%Y-%m-%d %H:%M %Z}:\n{roles}\n"
        "Find out why (the build, the rollout, a crashing pod) before telling "
        "the user it shipped; ergo_notify_when_live can watch again." + note
    )


def _notify(session: ConversationSession) -> None:
    from django_ergo.bots.todos import notify_session

    notify_session(session)


# -- the skill ---------------------------------------------------------------

NOTIFY_DESCRIPTION = """\
Get one automated event in this chat when a commit is actually running on this
server: every role (web, workers, beat, bots...) has a ready process reporting
that commit or a newer one containing it. Use it after a fix you asked for
merges, instead of self-wakes that poll; a merged PR or a built image is not
"live". If it isn't live within `hours`, you get a "not live" event naming what
is missing instead. A chat hears about each revision once."""

LIST_DESCRIPTION = """\
This chat's deploy watches, and what each role of this server runs right now
(from the processes' own reports)."""

CANCEL_DESCRIPTION = "Cancel one of this chat's pending deploy watches by id."


def live_toolkit(ctx: ToolContext) -> FunctionToolkit:
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import bot_tool

    @bot_tool(
        name="ergo_notify_when_live",
        description=NOTIFY_DESCRIPTION,
        parameters={
            "revision": {
                "type": "string",
                "description": "The commit SHA (full, or short where the server "
                "can resolve it) or image digest to wait for",
            },
            "note": {
                "type": "string",
                "description": "For your future self: what to verify or tell the "
                "user once it is live",
            },
            "hours": {
                "type": "number",
                "description": "Give up after this many hours (default "
                f"{api_settings.LIVE_WATCH_HOURS:g})",
            },
        },
        required=["revision"],
    )
    def notify(revision: str, note: str = "", hours: float | None = None) -> dict:
        w, created = watch(ctx.session, revision, note=note, hours=hours)
        now = verdict(w.revision)
        return {
            "watch": describe(w),
            "created": created,
            "live_now": now.live,
            "missing": now.missing(),
            "note": (
                "Armed. You'll get one event when it is live or the watch "
                "expires; end your turn instead of polling."
                if created
                else "This chat already waits for that revision."
            ),
        }

    @bot_tool(name="ergo_live_watches", description=LIST_DESCRIPTION, parameters={})
    def listing() -> dict:
        return {"watches": watches(ctx.session), "running": running()}

    @bot_tool(
        name="ergo_live_watch_cancel",
        description=CANCEL_DESCRIPTION,
        parameters={"watch_id": {"type": "string"}},
        required=["watch_id"],
    )
    def cancel_watch(watch_id: str) -> dict:
        done = cancel(ctx.session, watch_id)
        return {
            "cancelled": done,
            "note": "Cancelled." if done else "No pending watch with that id here.",
        }

    return FunctionToolkit(
        [notify.__bot_tool__, listing.__bot_tool__, cancel_watch.__bot_tool__], ctx
    )


def live_skills() -> list[SkillDef]:
    """The built-in ``deploys`` skill, when the host reports its processes."""
    if not enabled():
        return []
    from django_ergo.bots.skillset import SkillDef

    return [
        SkillDef(
            "deploys",
            "Get told once a merged change is actually running on this server "
            "(every role reports the commit), instead of polling",
            toolkits=lambda ctx: [live_toolkit(ctx)],
            source="built-in",
        )
    ]
