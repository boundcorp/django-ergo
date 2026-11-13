"""Versioned coordinator mail. Transport consumption is not model acceptance.

The ledger lives in Worker.args, independently of the watcher's state snapshot.
No send is automatically retried after a timeout or interrupted claim.
"""

from __future__ import annotations

import json
import shlex
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

from django_ergo.bots.workers import close_agent_mail
from django_ergo.bots.workers import schedule
from django_ergo.conversation.models import Worker

KEY = "_agent_instructions"
CHECKPOINT_GUIDANCE = """\
Coordinator instruction checkpoints (Ergo protocol v1):
Follow-up orchestration mail is NOT prompt injection. Check your inbox with the
original preamble's orchestration check command at safe checkpoints: before
external side effects, after bounded work/tool batches, after an ask returns,
before continuing after a stop, and before worker_done. Do not busy-poll.
Messages labelled Ergo instruction carry a monotonically increasing version and
explicit supersedes id. Drain available mail before acting, ignore superseded
versions, and prioritize urgent stop/cleanup at the next safe checkpoint; do not
interrupt processes or broaden authorization. A newer instruction only replaces
the older instruction, not the task's safety/authorization constraints.
For the latest instruction, send its requested status acknowledgement naming the
instruction id, version and concrete next action BEFORE proceeding. A read flag,
transport receipt or heartbeat is not this acknowledgement. Repeat acknowledgements
idempotently on replay; never re-execute side effects because mail was replayed.
"""


@contextmanager
def ledger(worker_id):
    with transaction.atomic():
        worker = Worker.objects.select_for_update().get(pk=worker_id)
        worker.args = dict(worker.args or {})
        rows = worker.args.setdefault(KEY, [])
        normalize_claims(rows)
        yield worker, rows
        worker.save(update_fields=["args", "updated_at"])


def enqueue(worker_id, instruction_id, text, supersedes="", priority="normal"):
    """Compare-and-append; the caller's stable id makes retries idempotent."""
    if not instruction_id.strip() or not text.strip():
        msg = "instruction_id and text must not be empty"
        raise ValueError(msg)
    if priority not in ("normal", "urgent"):
        msg = "priority must be normal or urgent"
        raise ValueError(msg)
    with ledger(worker_id) as (worker, rows):
        if not worker.active or worker.args.get("_agent_mail_closed"):
            msg = "The agent worker is no longer running"
            raise ValueError(msg)
        for row in rows:
            if row["id"] == instruction_id:
                if (row["text"], row["supersedes"], row["priority"]) != (
                    text,
                    supersedes,
                    priority,
                ):
                    msg = "instruction_id already names different content"
                    raise ValueError(msg)
                return dict(row)
        latest = rows[-1]["id"] if rows else ""
        if supersedes != latest:
            msg = f"supersedes must name the latest instruction: {latest!r}"
            raise ValueError(msg)
        row = {
            "schema_version": 1,
            "id": instruction_id,
            "version": len(rows) + 1,
            "supersedes": supersedes,
            "priority": priority,
            "text": text,
            "status": "queued",
            "created_at": timezone.now().isoformat(),
            "acknowledgement": None,
            "acknowledgement_claim": None,
            "acceptance": "unknown",
        }
        if rows:
            rows[-1]["superseded_by"] = instruction_id
        rows.append(row)
        transaction.on_commit(lambda: schedule(worker, 0))
        return dict(row)


def snapshot(worker_id):
    rows = (Worker.objects.get(pk=worker_id).args or {}).get(KEY, [])
    normalize_claims(rows)
    return {"latest": rows[-1] if rows else None, "instructions": rows}


def normalize_claims(rows):
    """Downgrade historical self-asserted acknowledgements on read and write."""
    for row in rows:
        claim = row.get("acknowledgement_claim")
        if not isinstance(claim, dict):
            old = row.get("acknowledgement")
            claim = dict(old) if isinstance(old, dict) else None
        if claim is not None:
            claim["verification"] = "unverified"
            claim.setdefault("claimed_sender", None)
            claim.setdefault("claimed_pane", None)
        row.update(
            acknowledgement=None,
            acknowledgement_claim=claim,
            acceptance="unknown",
        )


def body(row, handle):
    ack = json.dumps(
        {
            "instructionAck": {
                "id": row["id"],
                "version": row["version"],
                "nextAction": "REPLACE with your concrete next action",
            }
        }
    )
    command = shlex.join(
        [
            "orchestration",
            "send",
            "--type",
            "status",
            "--subject",
            "Instruction acknowledgement",
            "--body",
            ack,
            "--task-id",
            handle["task"],
            "--dispatch-id",
            handle["dispatch"],
        ]
    )
    return (
        f"Ergo instruction v{row['version']} id={row['id']} "
        f"supersedes={row['supersedes'] or '(none)'} priority={row['priority']}\n\n"
        f"{row['text']}\n\nRead all available instructions first; apply only the latest. "
        "Preserve the task's authorization constraints. At your next safe checkpoint, "
        "acknowledge the latest instruction with its id, version and a concrete next "
        "action (not a receipt). Use your preamble's CLI executable and worker "
        f"credentials with these arguments, replacing nextAction:\n{command}"
    )


def deliver(ctx, plugin, handle):
    """Durably claim once, then lock across preflight and send before replies."""
    with ledger(ctx.worker.pk) as (_, rows):
        if not rows:
            return
        row = rows[-1]
        if row["status"] == "processing":
            row["status"] = "unknown"
            row["error"] = (
                "Interrupted send; reconcile inbox, do not resend automatically"
            )
        if row["status"] != "queued":
            return
        row["status"] = "processing"
        claimed = dict(row)
    with ledger(ctx.worker.pk) as (worker, rows):
        row = next(r for r in rows if r["id"] == claimed["id"])
        if row["status"] != "processing":
            return
        if row.get("superseded_by"):
            row.update(status="unsent", error="Superseded before send")
            return
        attempted = False
        try:
            reason = worker.args.get("_agent_mail_closed")
            if not worker.active:
                reason = reason or "The agent worker is no longer running"
            if not reason:
                require_owner(handle)
                shown = plugin.cli_json(
                    ["orchestration", "worker-show", "--dispatch", handle["dispatch"]]
                )
                reason = terminal_reason(shown)
            if reason:
                # No send was attempted. Persist that distinction before the shared
                # closer classifies interrupted processing rows as uncertain.
                row.update(status="unsent", error=reason)
                worker.save(update_fields=["args", "updated_at"])
                close_agent_mail(worker.pk, reason)
                worker.refresh_from_db(fields=["args"])
                return
            attempted = True
            receipt = plugin.cli_json(
                [
                    "orchestration",
                    "send",
                    "--to",
                    f"dispatch:{handle['dispatch']}",
                    "--run",
                    handle["run"],
                    "--from",
                    handle["mailbox"],
                    "--type",
                    "status",
                    "--subject",
                    f"Ergo instruction v{claimed['version']}",
                    "--body",
                    body(claimed, handle),
                    "--priority",
                    claimed["priority"],
                    "--payload",
                    json.dumps(
                        {
                            "taskId": handle["task"],
                            "dispatchId": handle["dispatch"],
                            "ergoInstruction": claimed,
                        }
                    ),
                ]
            )
            row.update(status="transport_queued", receipt=receipt, error="")
        except Exception as exc:  # noqa: BLE001 — never replay an uncertain send
            row.update(
                status="unknown" if attempted else "unsent",
                error=f"{type(exc).__name__}: {exc}",
            )
        result = row["status"]
    ctx.tell(
        f"Instruction {claimed['id']} v{claimed['version']}: {result}. "
        "Model acceptance is unknown; use ergo_orca_instruction_status."
    )


def terminal_reason(shown):
    # Dispatch states mirror OrcaPlugin.SETTLED without importing the plugin
    # (which imports this protocol). Worker states use Orca's worker ledger.
    dispatch = str((shown.get("dispatch") or {}).get("status") or "")
    state = str((shown.get("worker") or {}).get("state") or "")
    resource = shown.get("terminalResource") or {}
    if dispatch in ("completed", "failed", "cancelled", "abandoned"):
        return f"Orca dispatch is settled: {dispatch}"
    if state in ("succeeded", "failed", "stopped", "abandoned", "released"):
        return f"Orca worker is no longer running: {state}"
    if resource.get("releaseState") in ("requested", "releasing", "released"):
        return f"Orca worker terminal release: {resource['releaseState']}"
    if not dispatch or not state:
        msg = (
            "Worker preflight lacks dispatch status or worker state; no send attempted"
        )
        raise ValueError(msg)
    return ""


def require_owner(handle):
    if not all(handle.get(k) for k in ("run", "mailbox", "task", "dispatch")):
        msg = "Missing dispatch-owned mailbox/run/task; reconcile ownership"
        raise ValueError(msg)


def object_value(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def record_transport(rows, message, payload, handle):
    outgoing = object_value(payload.get("ergoInstruction"))
    row = rows.get(outgoing.get("id")) if isinstance(outgoing.get("id"), str) else None
    if (
        not row
        or outgoing.get("version") != row["version"]
        or message.get("from_handle") != handle.get("mailbox")
    ):
        return
    row.update(
        message_id=message.get("id"),
        status="delivered" if message.get("delivered_at") else "transport_queued",
        transport_read=message.get("read"),
        delivered_at=message.get("delivered_at"),
    )


def record_ack(rows, message, shown):
    if message.get("type") != "status":
        return None
    ack = object_value(object_value(message.get("body")).get("instructionAck"))
    row = rows.get(ack.get("id")) if isinstance(ack.get("id"), str) else None
    action = ack.get("nextAction")
    if (
        not row
        or ack.get("version") != row["version"]
        or not isinstance(action, str)
        or not action.strip()
        or action.startswith("REPLACE")
        or row.get("acknowledgement_claim")
    ):
        return None
    dispatch = shown.get("dispatch") or {}
    sender = dispatch.get("assignee_handle")
    pane = dispatch.get("assignee_pane_key")
    # Orca status sends do not attest the caller: --from is caller-controlled,
    # and sender_pane_key can be looked up from that claimed handle. Matching
    # metadata is useful context, never authentication or model acceptance.
    row["acknowledgement"] = None
    row["acceptance"] = "unknown"
    row["acknowledgement_claim"] = {
        "verification": "unverified",
        "message_id": message["id"],
        "instruction_id": row["id"],
        "version": row["version"],
        "next_action": action,
        "claimed_sender": message.get("from_handle"),
        "claimed_pane": message.get("sender_pane_key"),
        "sender_matches_assignee": bool(
            sender and message.get("from_handle") == sender
        ),
        "pane_matches_assignee": bool(pane and message.get("sender_pane_key") == pane),
        "at": message.get("created_at"),
    }
    qualifier = "superseded" if row.get("superseded_by") else "latest"
    return (
        f"Unverified acknowledgement claim for instruction {row['id']} "
        f"v{row['version']} ({qualifier}); claimed next action: {action}. "
        "Sender metadata is not authenticated; model acceptance remains unknown."
    )


def reconcile(ctx, handle, shown, messages):
    """Non-consuming, bounded observation; status messages are unverified claims."""
    notices = []
    with ledger(ctx.worker.pk) as (_, rows):
        if not rows:
            return
        by_id = {row["id"]: row for row in rows}
        for message in messages:
            payload = object_value(message.get("payload"))
            if (
                message.get("run_id"),
                payload.get("dispatchId"),
                payload.get("taskId"),
            ) != (handle["run"], handle["dispatch"], handle.get("task")):
                continue
            record_transport(by_id, message, payload, handle)
            if notice := record_ack(by_id, message, shown):
                notices.append(notice)
    for notice in notices:
        ctx.tell(notice)


def tools(plugin):
    from django_ergo.bots.agents import worker_of
    from django_ergo.bots.plain_text import check_spacing
    from django_ergo.bots.tools import bot_tool

    def find(ctx, worker_id):
        worker = Worker.objects.filter(
            pk=worker_id, session=ctx.session, bot_name=ctx.bot.name
        ).first()
        found = worker_of(ctx.bot, worker) if worker else None
        if not found or found[0] is not plugin.manager:
            msg = "No Orca agent worker owned by this chat and environment"
            raise ValueError(msg)
        require_owner(found[1])
        return worker

    @bot_tool(
        name="ergo_orca_instruct",
        takes_context=True,
        description=(
            "Queue durable versioned coordinator instructions for this chat's Orca "
            "worker. Reuse instruction_id only for identical retries; supersedes must "
            "name the latest id (empty for first). Urgent stop/cleanup is cooperative "
            "at safe checkpoints, NOT a process interrupt or expanded authorization. "
            "Receipts and unverified acknowledgement claims are NOT model acceptance."
        ),
        parameters={
            "worker_id": {"type": "string"},
            "instruction_id": {
                "type": "string",
                "description": "Stable unique retry id",
            },
            "text": {"type": "string"},
            "supersedes": {
                "type": "string",
                "description": "Latest instruction id, or empty",
            },
            "priority": {"type": "string", "enum": ["normal", "urgent"]},
        },
        required=["worker_id", "instruction_id", "text", "supersedes"],
        requires_approval=plugin.approve_changes,
    )
    def instruct(ctx, worker_id, instruction_id, text, supersedes, priority="normal"):  # noqa: PLR0913
        worker = find(ctx, worker_id)
        check_spacing(text, "text")
        return enqueue(worker.pk, instruction_id, text, supersedes, priority)

    @bot_tool(
        name="ergo_orca_instruction_status",
        takes_context=True,
        description=(
            "Read the durable instruction ledger, including transport status and "
            "unverified acknowledgement claims and claimed next actions. Sender/pane "
            "metadata is not authentication; model acceptance remains unknown. "
            "No active delivery or polling."
        ),
        parameters={"worker_id": {"type": "string"}},
        required=["worker_id"],
    )
    def status(ctx, worker_id):
        return snapshot(find(ctx, worker_id).pk)

    return [instruct.__bot_tool__, status.__bot_tool__]
