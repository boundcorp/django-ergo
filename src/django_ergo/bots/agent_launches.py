"""Fail-closed Orca launch journal, independent of external request-recovery APIs.

An interrupted command may have had effects. Never replay it, even when no
external identity was returned. Only watcher registration is safe to retry.
"""

import json
from contextvars import ContextVar
from dataclasses import asdict
from hashlib import sha256
from uuid import uuid4

from django.db import transaction

from django_ergo.conversation.models import AgentLaunch

MAX_KEY_LENGTH = 200

_current = ContextVar("agent_launch", default=None)


def checkpoint(phase, *, handle=None, **evidence):
    """Commit known identities before the next operation can fail."""
    intent = _current.get()
    if intent is None:
        return
    intent.evidence = {**intent.evidence, **evidence, "phase": phase}
    if handle:
        intent.handle = {**intent.handle, **handle}
    intent.save(update_fields=["evidence", "handle", "updated_at"])


def prepare(state, title):
    intent = _current.get()
    if intent is not None:
        intent.watch_state = dict(state)
        intent.watch_title = title
        intent.save(update_fields=["watch_state", "watch_title", "updated_at"])


def register(ctx, handle, state, title):
    intent = _current.get()
    if intent is None:
        return None
    # Persist the receipt separately: a failed registration transaction cannot
    # roll it back. A crashed caller can replay without calling manager.start.
    intent.handle = {**intent.handle, **handle}
    intent.watch_state = dict(state)
    intent.watch_title = title
    intent.status = "watch_needed"
    intent.save(
        update_fields=["handle", "watch_state", "watch_title", "status", "updated_at"]
    )
    return _watch(ctx, intent.pk)


def _watch(ctx, intent_id):
    with transaction.atomic():
        intent = AgentLaunch.objects.select_for_update().get(pk=intent_id)
        if intent.worker_id:
            if intent.status != "watching":
                intent.status = "watching"
                intent.save(update_fields=["status", "updated_at"])
            return intent.worker
        if not intent.handle.get("dispatch"):
            msg = "Launch has no known dispatch; operator reconciliation required"
            raise ValueError(msg)
        worker = ctx.workers.start(
            "agent:orca",
            title=intent.watch_title or "Recovered Orca launch",
            state=intent.watch_state,
            handle=intent.handle,
        )
        intent.worker = worker
        intent.status = "watching"
        intent.save(update_fields=["worker", "status", "updated_at"])
    return worker


def launch(ctx, spec, manager, execute):
    from django_ergo.bots.agents import authority_reference
    from django_ergo.bots.agents import superseded_worker_reference

    # Revalidate original chat and authenticated reference on every recovery.
    if str(getattr(ctx.user, "pk", "")) != str(ctx.session.user_id):
        msg = "Agent launch requires this chat's owner context"
        raise ValueError(msg)
    authority = authority_reference(ctx, spec.authority_message_id)
    superseded_worker_reference(ctx, spec.supersedes_worker_id)
    key = spec.launch_key or str(uuid4())
    if len(key) > MAX_KEY_LENGTH:
        msg = "launch_key exceeds 200 characters"
        raise ValueError(msg)
    request = {
        "spec": asdict(spec),
        "owner": str(ctx.session.user_id),
        "authority": authority,
        "manager": manager.name,
        "session": str(ctx.session.pk),
        # Bind the admission configuration as well as the caller's workspace.
        "scope": {
            "environment": manager.plugin.environment,
            "repositories": manager.plugin.scoped_repositories,
            "agents": list(manager.agents),
            "files_host": manager.plugin.files_host,
            "executable": manager.plugin.executable,
        },
    }
    request["spec"]["launch_key"] = key
    digest = sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    # durable=True refuses an outer transaction that could roll back an intent
    # after an external effect. No transaction is held during Orca commands.
    with transaction.atomic(durable=True):
        intent, created = AgentLaunch.objects.get_or_create(
            session=ctx.session,
            bot_name=ctx.bot.name,
            key=key,
            defaults={
                "request": request,
                "spec_hash": digest,
                "handle": {"launch_key": key},
                "evidence": {
                    "approval_reference": {
                        field: str(value)
                        for field, value in (ctx.approval_decision or {}).items()
                        if field in {"id", "actor_id", "call_id", "tool_use_id"}
                    },
                    "automatically_approved": ctx.auto_approved,
                },
            },
        )
    if not created:
        if intent.spec_hash != digest:
            msg = "launch_key already binds a different spec or authorization scope"
            raise ValueError(msg)
        if intent.worker_id:
            # Registration may have committed before a notification/publish
            # callback failed. Requeue the existing row, never create a new one.
            from django_ergo.bots.workers import schedule

            schedule(intent.worker, 0)
            return intent.worker
        if intent.handle.get("dispatch"):
            return _watch(ctx, intent.pk)
        raise ValueError(_blocked(intent))
    token = _current.set(intent)
    try:
        return execute()
    except Exception as exc:
        # Do not stop: a failed stop cannot prove termination, and the original
        # dispatch must remain observable and recoverable.
        intent.refresh_from_db()
        if not intent.worker_id:
            intent.status = "recovery_needed"
            intent.save(update_fields=["status", "updated_at"])
        if not intent.evidence.get("phase"):
            raise
        msg = f"{exc}. {_blocked(intent)}"
        raise ValueError(msg) from exc
    finally:
        _current.reset(token)


def _blocked(intent):
    return (
        f"Orca launch {intent.pk} (launch_key={intent.key}) requires recovery; "
        f"phase={intent.evidence.get('phase', 'validation')}. "
        "Do not issue a replacement start. "
        + (
            "Retry the same key and exact original arguments to register/watch the known dispatch."
            if intent.handle.get("dispatch")
            else "Operator: inspect this AgentLaunch journal and reconcile its exact worktree, "
            "task and Run with Orca worker-show/task-list; retain blocked state if the "
            "original dispatch cannot be established. No request recovery API is assumed."
        )
    )
