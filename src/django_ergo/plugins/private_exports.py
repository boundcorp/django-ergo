"""Explicit, authenticated exports of exact briefs and immutable file snapshots.

This is an application policy, not a sandbox for shell or custom Python tools.
"""

import hashlib
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from django_ergo.bots import messaging
from django_ergo.bots.orchestrator import _target
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.attachments import find_session_file
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.budget import forbid
from django_ergo.conversation.context import TextContextSource
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import PrivateExport
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.structured import _current_pending_review
from django_ergo.conversation.toolkit import approval_pending_digest

MAX_FILE_BYTES = 5_000_000
MAX_FILES = 10
MAX_BRIEF_CHARS = 8000
APPROVAL_LIFETIME = timedelta(minutes=15)


class PrivateExportsPlugin(BotPlugin):
    name = "private_exports"
    description = (
        "Export an exact brief and selected file snapshots with human approval"
    )

    def on_load(self):
        if not self.bot.definition.private:
            msg = "private_exports requires private: true"
            raise ValueError(msg)

    @property
    def skill_instructions(self):
        return (
            "Private outward sends and forwards are blocked. Prepare a minimal brief "
            "and selected files with ergo_private_export_prepare, inspect the selected "
            "files with the user, then pass its exact returned arguments to "
            "ergo_private_export. The user must approve that exact destination, text "
            "and file digests. Never use shell, client, database, filesystem or other "
            "tool fallbacks to evade export approval. Shell/custom tools are not a "
            "hardened execution boundary. Snapshots cannot be forwarded or updated."
        )

    def always_context_sources(self, ctx, message):
        return [TextContextSource("Private exports", self.skill_instructions)]

    def _destination(self, ctx, destination):
        recipient = ConversationSession.objects.filter(
            pk=destination, user_id=ctx.session.user_id
        ).first()
        if recipient is None or recipient.bot_name == ctx.session.bot_name:
            msg = "Export needs an existing destination chat of another callable bot."
            raise ValueError(msg)
        _target(ctx, recipient.bot_name, str(recipient.pk))
        return recipient

    def _files(self, ctx, artifacts):
        if not isinstance(artifacts, list) or len(artifacts) > MAX_FILES:
            msg = "Export at most ten selected files."
            raise ValueError(msg)
        selected = []
        seen = set()
        for item in artifacts:
            if not isinstance(item, dict) or set(item) != {
                "id",
                "sha256",
                "filename",
                "media_type",
            }:
                msg = "Invalid export artifact manifest; prepare it again."
                raise ValueError(msg)
            row = find_session_file(ctx.session, item["id"])
            if str(row.pk) in seen or row.private_export_id or not row.file or row.url:
                msg = "Export only distinct local source files, not URLs or snapshots."
                raise ValueError(msg)
            seen.add(str(row.pk))
            with row.file.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                msg = "Export files must be at most 5 MB."
                raise ValueError(msg)
            actual = {
                "id": str(row.pk),
                "sha256": hashlib.sha256(data).hexdigest(),
                "filename": row.filename,
                "media_type": row.media_type,
            }
            if actual != item:
                msg = "Export content changed; prepare a new export and approval."
                raise ValueError(msg)
            selected.append((row, data))
        return selected

    def prepare(self, ctx, destination, message, attachments=None):
        self._destination(ctx, destination)
        artifacts = []
        if len(attachments or []) > MAX_FILES:
            msg = "Export at most ten selected files."
            raise ValueError(msg)
        for ref in attachments or []:
            row = find_session_file(ctx.session, ref)
            if not row.file:
                msg = "Export needs stored file bytes, not a URL."
                raise ValueError(msg)
            with row.file.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            artifacts.append(
                {
                    "id": str(row.pk),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "filename": row.filename,
                    "media_type": row.media_type,
                }
            )
        args = {
            "destination": str(destination),
            "message": message,
            "artifacts": artifacts,
        }
        self.preview(ctx, **args)
        return args

    def preview(self, ctx, destination, message, artifacts):
        recipient = self._destination(ctx, destination)
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > MAX_BRIEF_CHARS
        ):
            msg = "Export an exact nonempty brief of at most 8000 characters."
            raise ValueError(msg)
        selected = self._files(ctx, artifacts)
        files = "\n".join(
            f"{row.filename} ({row.media_type}, {len(data)} bytes), "
            f"source {row.pk}, SHA-256 {item['sha256']}"
            for (row, data), item in zip(selected, artifacts, strict=True)
        )
        return (
            f"Export to {recipient.bot_name}, exact chat {recipient.pk}.\n"
            f"Exact brief:\n{message}\n\nSelected immutable snapshots:\n{files or '(none)'}\n"
            "No source history or other files are included. Review the selected source "
            "files before approving their digests. Approval expires after 15 minutes "
            "from the turn's creation; changes require a new export."
        )

    def export(self, ctx, destination, message, artifacts):
        forbid("export private content")
        evidence = ctx.approval_decision
        if not evidence or not all(
            evidence.get(k) for k in ("id", "actor_id", "call_id", "tool_use_id")
        ):
            msg = "Private export requires authenticated human approval."
            raise ValueError(msg)
        args = {"destination": destination, "message": message, "artifacts": artifacts}
        with transaction.atomic():
            call = StructuredCall.objects.select_for_update().get(
                pk=evidence["call_id"]
            )
            pending = (call.metadata or {}).get("pending_approvals", [])
            batch = next(
                (
                    b
                    for b in (call.metadata or {}).get(
                        "authenticated_approval_decisions", []
                    )
                    if b.get("id") == evidence["id"]
                ),
                {},
            )
            item = next(
                (p for p in pending if p.get("id") == evidence["tool_use_id"]), {}
            )
            if (
                call.session_id != ctx.session.pk
                or str(ctx.session.user_id) != str(evidence["actor_id"])
                or call.created_at < timezone.now() - APPROVAL_LIFETIME
                or batch.get("shown_verified") is not True
                or batch.get("call_id") != str(call.pk)
                or batch.get("actor_id") != evidence["actor_id"]
                or batch.get("decisions", {}).get(evidence["tool_use_id"]) is not True
                or batch.get("pending_sha256") != approval_pending_digest(pending)
                or item.get("name") != "ergo_private_export"
                or item.get("input") != args
                or not _current_pending_review(
                    call.pk, pending, claimed=True, tool_id=evidence["tool_use_id"]
                )
                or PrivateExport.objects.filter(
                    call=call, tool_use_id=evidence["tool_use_id"]
                ).exists()
            ):
                msg = "Private export approval is stale, replayed or does not match."
                raise ValueError(msg)
            # Recheck current permissions and actual bytes before any destination effect.
            self.preview(ctx, **args)
            recipient = self._destination(ctx, destination)
            selected = self._files(ctx, artifacts)
            receipt = PrivateExport.objects.create(
                source=ctx.session,
                destination=recipient,
                call=call,
                authorization_id=evidence["id"],
                actor_id=evidence["actor_id"],
                tool_use_id=evidence["tool_use_id"],
                manifest_sha256=approval_pending_digest(args),
                artifacts=artifacts,
            )
            copies = []
            for row, data in selected:
                copy = save_session_file(
                    recipient,
                    row.filename,
                    data,
                    media_type=row.media_type,
                    source="bot",
                    metadata={"private_export": str(receipt.pk)},
                )
                copy.private_export = receipt
                copy.save(update_fields=["private_export"])
                copies.append(copy)
            shared = [messaging.shared_file(row) for row in copies]
            token = messaging.private_export_delivery.set(
                (
                    str(ctx.session.pk),
                    str(recipient.pk),
                    message,
                    tuple(str(row.pk) for row in copies),
                )
            )
            try:
                sent = messaging.send(
                    ctx.session,
                    recipient,
                    message,
                    registry=ctx.bot.registry,
                    metadata={"attachments": shared, "private_export": str(receipt.pk)},
                )
            finally:
                messaging.private_export_delivery.reset(token)
            receipt.result = sent
            receipt.save(update_fields=["result"])
        return {
            "export_id": str(receipt.pk),
            "message_id": str(sent.pk),
            "thread_id": str(recipient.pk),
            "attachments": [str(row.pk) for row in copies],
        }

    def toolkits(self, ctx):
        plugin = self

        @bot_tool(name="ergo_private_export_prepare", takes_context=True)
        def prepare(
            ctx, destination: str, message: str, attachments: list[str] | None = None
        ):
            """Prepare exact arguments for exporting a brief and selected own files; no delivery."""
            return plugin.prepare(ctx, destination, message, attachments)

        @bot_tool(
            name="ergo_private_export",
            takes_context=True,
            requires_approval=True,
            approval_preview=plugin.preview,
        )
        def export(ctx, destination: str, message: str, artifacts: list[dict]):
            """Export the exact prepared brief and file digests to one existing chat, with human approval."""
            return plugin.export(ctx, destination, message, artifacts)

        return [FunctionToolkit.from_functions([prepare, export], ctx)]
